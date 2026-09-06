"""Provider-call evidence for durable intent dispatch; no real gateway I/O."""

import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest

from getpaid_core import PluginRegistry
from getpaid_core.durable import DurablePaymentFlow
from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import PaymentFacts
from getpaid_core.exceptions import GetPaidException
from tests.conftest import MockProcessor


NOW = datetime(2026, 9, 6, tzinfo=UTC)


async def test_legacy_processor_is_refused_before_reservation_or_submission():
    registry = PluginRegistry()
    registry._discovered = True
    registry.register(MockProcessor)
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                backend=MockProcessor.slug,
                remaining_authorization=Decimal("100"),
                status="pre-auth",
            )
        ]
    )
    flow = DurablePaymentFlow(repository, registry=registry)
    with pytest.raises(GetPaidException, match=r"durable.*capabilit"):
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
            return OperationOutcome(
                OperationState.PROVIDER_PENDING, correlation="charge-1"
            )

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
    assert (
        await repository.get_operation("pay", "capture")
    ).correlation == "charge-1"


async def test_response_loss_is_unknown_then_authoritative_lookup_settles_once():
    from getpaid_core.durable.provider import LookupSemantics
    from getpaid_core.durable.provider import OperationCapabilities

    calls = []
    lookups = []

    class LostResponse(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="merchant",
                idempotency_window=timedelta(hours=1),
                lookup_semantics=LookupSemantics.AUTHORITATIVE,
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            calls.append(operation)
            raise TimeoutError("response lost after provider transaction")

        @classmethod
        async def lookup_operation(cls, operation, *, config):
            lookups.append(operation)
            return OperationOutcome(
                OperationState.SUCCEEDED, correlation="charge-1"
            )

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


async def test_safe_retry_reuses_the_key_and_payload_after_reconciling():
    from getpaid_core.durable.provider import LookupSemantics
    from getpaid_core.durable.provider import OperationCapabilities

    calls, events, transactions = [], [], set()

    class Idempotent(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="merchant",
                idempotency_window=timedelta(hours=1),
                lookup_semantics=LookupSemantics.AUTHORITATIVE,
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            events.append("submit")
            calls.append(operation)
            transactions.add(operation.idempotency_key)
            if len(calls) == 1:
                raise TimeoutError
            return OperationOutcome(OperationState.SUCCEEDED)

        @classmethod
        async def lookup_operation(cls, operation, *, config):
            events.append("lookup")
            return OperationOutcome(OperationState.UNKNOWN)

    _, flow = make_flow(Idempotent)
    await flow.execute_operation(
        "pay",
        OperationIntent(
            "capture",
            OperationType.CHARGE,
            parameters={"nested": {"items": ["a"]}},
        ),
        now=NOW,
    )
    result = await flow.reconcile_operation(
        "pay", "capture", now=NOW, resubmit=True
    )
    assert result.outcome is OperationState.SUCCEEDED
    assert events == ["submit", "lookup", "submit"]
    assert len(transactions) == 1
    assert calls[0].parameters == calls[1].parameters
    assert (
        calls[0].resolved_amount == calls[1].resolved_amount == Decimal("100")
    )
    assert calls[0].submitted_at == calls[1].submitted_at
    assert calls[0].retry_until == calls[1].retry_until


async def test_final_write_failure_exposes_safe_identity_and_keeps_recovery_anchor():
    from getpaid_core.durable.provider import OperationCapabilities
    from getpaid_core.exceptions import OperationPersistenceError

    class FailingRepository(InMemoryDurableRepository):
        async def record_operation_outcome(self, *args):
            raise OSError("database unavailable")

    calls = []

    class Successful(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="merchant",
                idempotency_window=timedelta(hours=1),
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            calls.append(operation)
            return OperationOutcome(
                OperationState.SUCCEEDED, correlation="charge-1"
            )

    repository = FailingRepository(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                backend=Successful.slug,
                remaining_authorization=Decimal("100"),
                status="pre-auth",
            )
        ]
    )
    _, flow = make_flow(Successful, repository=repository)
    intent = OperationIntent("capture", OperationType.CHARGE)
    with pytest.raises(OperationPersistenceError) as caught:
        await flow.execute_operation("pay", intent, now=NOW)
    assert caught.value.context == {
        "payment_id": "pay",
        "operation_id": "capture",
        "operation_type": "charge",
        "correlation": "charge-1",
    }
    assert caught.value.provider_resubmission_allowed is False
    assert isinstance(caught.value.__cause__, OSError)
    operations = await repository.list_unresolved_operations()
    assert len(operations) == 1
    assert operations[0].state is OperationState.SUBMITTING
    assert (
        await flow.execute_operation("pay", intent, now=NOW)
    ).outcome is OperationState.SUBMITTING
    assert len(calls) == 1


@pytest.mark.parametrize(
    "bad_outcome",
    [
        None,
        "secret raw payload",
        OperationOutcome(
            OperationState.SUCCEEDED, settled_amount=Decimal("101")
        ),
    ],
)
async def test_invalid_provider_evidence_is_not_a_persistence_error_or_rejection(
    bad_outcome,
):
    from getpaid_core.durable.provider import OperationCapabilities
    from getpaid_core.exceptions import OperationEvidenceError

    class InvalidProvider(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="merchant",
                idempotency_window=timedelta(hours=1),
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            return bad_outcome

    repository, flow = make_flow(InvalidProvider)
    with pytest.raises(OperationEvidenceError) as caught:
        await flow.execute_operation(
            "pay", OperationIntent("capture", OperationType.CHARGE), now=NOW
        )
    assert caught.value.context["operation_id"] == "capture"
    assert "secret raw payload" not in str(caught.value)
    assert caught.value.provider_resubmission_allowed is False
    assert (await repository.get_payment_facts("pay")).captured_funds == 0
    assert (await repository.list_unresolved_operations())[
        0
    ].state is OperationState.SUBMITTING


async def test_declared_capability_without_submission_implementation_is_refused():
    from getpaid_core.durable import OperationCapabilities
    from getpaid_core.exceptions import UnsupportedProcessorError

    class DeclarationOnly(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="merchant",
                idempotency_window=timedelta(hours=1),
            )
        }

    repository, flow = make_flow(DeclarationOnly)
    with pytest.raises(UnsupportedProcessorError):
        await flow.execute_operation(
            "pay", OperationIntent("capture", OperationType.CHARGE), now=NOW
        )
    assert await repository.get_operation("pay", "capture") is None


async def test_provider_wait_is_bounded_and_cancellation_remains_discoverable():
    from getpaid_core.durable import OperationCapabilities

    entered = asyncio.Event()

    class Waiting(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="merchant",
                idempotency_window=timedelta(hours=1),
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            entered.set()
            await asyncio.Event().wait()

    _, timeout_flow = make_flow(Waiting, provider_timeout=0.001)
    result = await timeout_flow.execute_operation(
        "pay", OperationIntent("timed-out", OperationType.CHARGE), now=NOW
    )
    assert result.outcome is OperationState.UNKNOWN
    entered.clear()
    repository, flow = make_flow(Waiting)
    task = asyncio.create_task(
        flow.execute_operation(
            "pay", OperationIntent("cancelled", OperationType.CHARGE), now=NOW
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await repository.list_unresolved_operations())[
        0
    ].state is OperationState.SUBMITTING


@pytest.mark.parametrize("stage", ["reservation", "submission_claim"])
async def test_failed_pre_submission_write_never_reaches_provider(stage):
    from getpaid_core.durable import OperationCapabilities

    calls = []

    class Recording(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="merchant",
                idempotency_window=timedelta(hours=1),
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            calls.append(operation)
            return OperationOutcome(OperationState.SUCCEEDED)

    class FailingRepository(InMemoryDurableRepository):
        async def reserve_operation(self, payment_id, intent):
            if stage == "reservation":
                raise OSError("reservation failure")
            return await super().reserve_operation(payment_id, intent)

        async def claim_submission(self, *args, **kwargs):
            raise OSError("submission claim failure")

    repository = FailingRepository(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                backend=Recording.slug,
                remaining_authorization=Decimal("100"),
                status="pre-auth",
            )
        ]
    )
    _, flow = make_flow(Recording, repository=repository)
    with pytest.raises(OSError):
        await flow.execute_operation(
            "pay", OperationIntent("capture", OperationType.CHARGE), now=NOW
        )
    assert calls == []
    operation = await repository.get_operation("pay", "capture")
    if stage == "reservation":
        assert operation is None
    else:
        assert operation.state is OperationState.RESERVED


async def test_existing_intent_is_readable_after_provider_capability_is_disabled():
    from getpaid_core.durable import OperationCapabilities

    class Recording(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="merchant",
                idempotency_window=timedelta(hours=1),
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            return OperationOutcome(OperationState.SUCCEEDED)

    _, flow = make_flow(Recording)
    intent = OperationIntent("capture", OperationType.CHARGE)
    await flow.execute_operation("pay", intent, now=NOW)
    Recording.operation_capabilities = {}
    result = await flow.execute_operation("pay", intent, now=NOW)
    assert result.outcome is OperationState.SUCCEEDED
    assert result.snapshot.captured_funds == Decimal("100")


@pytest.mark.parametrize(
    "operation_type,facts_changes",
    [
        (
            OperationType.PREPARE,
            {
                "captured_funds": Decimal("100"),
                "remaining_authorization": Decimal("0"),
                "status": "paid",
            },
        ),
        (OperationType.RELEASE_LOCK, {"captured_funds": Decimal("101")}),
        (
            OperationType.START_REFUND,
            {
                "captured_funds": Decimal("100"),
                "refunded_funds": Decimal("-1"),
                "remaining_authorization": Decimal("0"),
                "status": "paid",
            },
        ),
    ],
)
async def test_ineligible_or_corrupt_current_facts_are_refused_before_io(
    operation_type, facts_changes
):
    from dataclasses import replace

    from getpaid_core.durable import OperationCapabilities
    from getpaid_core.exceptions import InvalidTransitionError

    calls = []

    class Recording(MockProcessor):
        operation_capabilities = dict.fromkeys(
            OperationType,
            OperationCapabilities(
                idempotency_scope="merchant",
                idempotency_window=timedelta(hours=1),
            ),
        )

        @classmethod
        async def submit_operation(cls, operation, *, config):
            calls.append(operation)
            return OperationOutcome(OperationState.SUCCEEDED)

    facts = PaymentFacts(
        "pay",
        Decimal("100"),
        backend=Recording.slug,
        remaining_authorization=Decimal("100"),
        status="pre-auth",
    )
    repository = InMemoryDurableRepository([replace(facts, **facts_changes)])
    _, flow = make_flow(Recording, repository=repository)
    with pytest.raises(InvalidTransitionError):
        await flow.execute_operation(
            "pay", OperationIntent("bad", operation_type), now=NOW
        )
    assert calls == []
    assert await repository.get_operation("pay", "bad") is None


@pytest.mark.parametrize("slow_attempt", [0, 1], ids=["initial", "retry"])
@pytest.mark.parametrize(
    "claim_seconds", [40, 62], ids=["insufficient-call-budget", "expired"]
)
async def test_slow_claim_cannot_submit_outside_idempotency_window(
    monkeypatch, slow_attempt, claim_seconds
):
    from getpaid_core.durable import OperationCapabilities

    elapsed = [0.0]
    monkeypatch.setattr(
        "getpaid_core.durable.flow.monotonic", lambda: elapsed[0]
    )
    calls = []

    class Recording(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="merchant",
                idempotency_window=timedelta(seconds=60),
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            calls.append(operation)
            raise TimeoutError

    class SlowClaim(InMemoryDurableRepository):
        async def claim_submission(self, *args, **kwargs):
            claim = await super().claim_submission(*args, **kwargs)
            if kwargs["expected_attempt"] == slow_attempt:
                elapsed[0] += claim_seconds
            return claim

    repository = SlowClaim(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                backend=Recording.slug,
                remaining_authorization=Decimal("100"),
                status="pre-auth",
            )
        ]
    )
    _, flow = make_flow(Recording, repository=repository)
    result = await flow.execute_operation(
        "pay", OperationIntent("capture", OperationType.CHARGE), now=NOW
    )
    if slow_attempt:
        result = await flow.reconcile_operation(
            "pay", "capture", now=NOW, resubmit=True
        )
    assert len(calls) == slow_attempt
    assert result.outcome is OperationState.SUBMITTING
    assert result.snapshot.captured_funds == 0
    assert len(await repository.list_unresolved_operations()) == 1


def make_flow(processor, *, repository=None, **options):
    registry = PluginRegistry()
    registry._discovered = True
    registry.register(processor)
    if repository is None:
        repository = InMemoryDurableRepository(
            [
                PaymentFacts(
                    "pay",
                    Decimal("100"),
                    backend=processor.slug,
                    remaining_authorization=Decimal("100"),
                    status="pre-auth",
                )
            ]
        )
    return repository, DurablePaymentFlow(
        repository, registry=registry, **options
    )
