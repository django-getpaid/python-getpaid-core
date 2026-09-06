"""Provider-call evidence for durable intent dispatch; no real gateway I/O."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncio

import pytest

from getpaid_core import PluginRegistry
from getpaid_core.durable import DurablePaymentFlow, InMemoryDurableRepository
from getpaid_core.durable import OperationIntent, OperationOutcome, OperationState
from getpaid_core.durable import OperationType, PaymentFacts
from getpaid_core.exceptions import GetPaidException
from tests.conftest import MockProcessor


NOW = datetime(2026, 9, 6, tzinfo=UTC)


async def test_legacy_processor_is_refused_before_reservation_or_submission():
    registry = PluginRegistry()
    registry._discovered = True
    registry.register(MockProcessor)
    repository = InMemoryDurableRepository([
        PaymentFacts("pay", Decimal("100"), backend=MockProcessor.slug,
                     remaining_authorization=Decimal("100"), status="pre-auth")
    ])
    flow = DurablePaymentFlow(repository, registry=registry)
    with pytest.raises(GetPaidException, match="durable.*capabilit"):
        await flow.execute_operation(
            "pay", OperationIntent("capture", OperationType.CHARGE), now=NOW
        )
    assert await repository.get_operation("pay", "capture") is None


async def test_sequential_retry_returns_the_pending_intent_without_resubmitting():
    from getpaid_core.durable.provider import OperationCapabilities

    calls = []

    class Recording(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="merchant account",
                idempotency_window=timedelta(hours=24),
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            calls.append(operation)
            return OperationOutcome(OperationState.PROVIDER_PENDING, correlation="charge-1")

    repository, flow = make_flow(Recording)
    intent = OperationIntent("capture", OperationType.CHARGE)
    first = await flow.execute_operation("pay", intent, now=NOW)
    second = await flow.execute_operation("pay", intent, now=NOW)
    assert len(calls) == 1
    assert calls[0].state is OperationState.SUBMITTING
    assert calls[0].resolved_amount == Decimal("100")
    assert calls[0].idempotency_key
    assert second.operation_id == first.operation_id == "capture"
    assert second.outcome is OperationState.PROVIDER_PENDING
    assert second.snapshot.captured_funds == 0
    assert (await repository.get_operation("pay", "capture")).correlation == "charge-1"


async def test_response_loss_is_unknown_then_authoritative_lookup_settles_once():
    from getpaid_core.durable.provider import LookupSemantics, OperationCapabilities

    calls = []
    lookups = []

    class LostResponse(MockProcessor):
        operation_capabilities = {OperationType.CHARGE: OperationCapabilities(
            idempotency_scope="merchant", idempotency_window=timedelta(hours=1),
            lookup_semantics=LookupSemantics.AUTHORITATIVE,
        )}

        @classmethod
        async def submit_operation(cls, operation, *, config):
            calls.append(operation)
            raise TimeoutError("response lost after provider transaction")

        @classmethod
        async def lookup_operation(cls, operation, *, config):
            lookups.append(operation)
            return OperationOutcome(OperationState.SUCCEEDED, correlation="charge-1")

    repository, flow = make_flow(LostResponse)
    intent = OperationIntent("capture", OperationType.CHARGE)
    result = await flow.execute_operation("pay", intent, now=NOW)
    assert result.outcome is OperationState.UNKNOWN
    assert result.snapshot.captured_funds == 0
    duplicate = await flow.execute_operation("pay", intent, now=NOW)
    assert duplicate.outcome is OperationState.UNKNOWN
    result = await flow.reconcile_operation("pay", "capture", now=NOW)
    assert result.outcome is OperationState.SUCCEEDED
    assert result.snapshot.captured_funds == Decimal("100")
    assert len(calls) == len(lookups) == 1
    assert await repository.list_unresolved_operations() == ()


def make_flow(processor, *, repository=None, **options):
    registry = PluginRegistry()
    registry._discovered = True
    registry.register(processor)
    if repository is None:
        repository = InMemoryDurableRepository([
            PaymentFacts("pay", Decimal("100"), backend=processor.slug,
                         remaining_authorization=Decimal("100"), status="pre-auth")
        ])
    return repository, DurablePaymentFlow(repository, registry=registry, **options)
