"""PUSH/PULL and submission ordering through the real durable flow, with fakes."""

import asyncio
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest

from getpaid_core import PluginRegistry
from getpaid_core.durable import DurablePaymentFlow
from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import OperationCapabilities
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import PaymentFacts
from getpaid_core.durable.records import PaymentObservation
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.types import PaymentUpdate
from tests.conftest import MockPayment
from tests.conftest import MockProcessor


@pytest.mark.parametrize(
    "refunded,status",
    [
        ("0", PaymentStatus.REFUND_STARTED),
        ("30", PaymentStatus.PARTIALLY_REFUNDED),
        ("100", PaymentStatus.REFUNDED),
    ],
)
@pytest.mark.parametrize("new_total", ["80", "100", "120"])
@pytest.mark.parametrize("pull_first", [False, True])
async def test_callback_pull_ordering_keeps_current_refund_facts(
    refunded, status, new_total, pull_first
):
    class Observing(MockProcessor):
        async def handle_callback(self, data, headers, **kwargs):
            return PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal(new_total),
            )

        async def fetch_payment_status(self, **kwargs):
            return PaymentUpdate(
                payment_event=PaymentEvent.PAYMENT_CAPTURED,
                paid_amount=Decimal("100"),
                provider_event_id="pull",
            )

    registry = PluginRegistry()
    registry._discovered = True
    registry.register(Observing)
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "pay-1",
                Decimal("150"),
                backend="mock",
                captured_funds=Decimal("100"),
                refunded_funds=Decimal(refunded),
                status=status,
            )
        ]
    )
    flow = DurablePaymentFlow(repository, registry=registry)
    callback_snapshot = MockPayment(status=PaymentStatus.PREPARED)
    pull_snapshot = MockPayment(status=PaymentStatus.PREPARED)
    if pull_first:
        await flow.fetch_and_update_status(pull_snapshot)
        await flow.handle_callback(callback_snapshot, {}, {})
    else:
        await flow.handle_callback(callback_snapshot, {}, {})
        await flow.fetch_and_update_status(pull_snapshot)
    facts = await repository.get_payment_facts("pay-1")
    assert facts.captured_funds == Decimal(
        "120" if new_total == "120" else "100"
    )
    assert facts.refunded_funds == Decimal(refunded)
    expected = (
        PaymentStatus.PARTIALLY_REFUNDED
        if status == PaymentStatus.REFUNDED and new_total == "120"
        else status
    )
    assert facts.status == expected
    assert facts.reconciliation_required is (new_total == "120")
    assert callback_snapshot.amount_paid == pull_snapshot.amount_paid == 0


async def test_callback_completes_while_submission_response_is_in_flight():
    submitted = asyncio.Event()
    respond = asyncio.Event()
    calls = []

    class Racing(MockProcessor):
        operation_capabilities = {
            OperationType.CHARGE: OperationCapabilities(
                idempotency_scope="payment-operation",
                idempotency_window=timedelta(hours=1),
            )
        }

        @classmethod
        async def submit_operation(cls, operation, *, config):
            calls.append(operation.operation_id)
            submitted.set()
            await respond.wait()
            return OperationOutcome(
                OperationState.PROVIDER_PENDING, correlation="capture-handle"
            )

        async def handle_callback(self, data, headers, **kwargs):
            return PaymentObservation(
                operation_id="capture",
                outcome=OperationOutcome(
                    OperationState.SUCCEEDED, correlation="capture-handle"
                ),
                paid_amount=Decimal("40"),
                provider_event_id="callback",
            )

    registry = PluginRegistry()
    registry._discovered = True
    registry.register(Racing)
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "pay-1",
                Decimal("100"),
                backend="mock",
                remaining_authorization=Decimal("100"),
                status=PaymentStatus.PRE_AUTH,
            )
        ]
    )
    flow = DurablePaymentFlow(repository, registry=registry)
    async with asyncio.timeout(5), asyncio.TaskGroup() as group:
        task = group.create_task(
            flow.execute_operation(
                "pay-1",
                OperationIntent("capture", OperationType.CHARGE, Decimal("40")),
                now=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        await submitted.wait()
        callback = await flow.handle_callback(MockPayment(), {}, {})
        assert callback.operations[0].state == OperationState.SUCCEEDED
        respond.set()
    assert task.result().operation.state == OperationState.SUCCEEDED
    assert task.result().snapshot.captured_funds == Decimal("40")
    assert calls == ["capture"]
