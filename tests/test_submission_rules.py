"""Durable submission ownership, immutable requests, and outcome ordering."""

import asyncio
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest

from getpaid_core.durable.memory import InMemoryDurableRepository
from getpaid_core.durable.records import CANCELLATION_TARGET
from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationState
from getpaid_core.durable.records import OperationType
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.rules import plan_outcome
from getpaid_core.durable.rules import plan_reservation
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationConflictError


def authorized_facts(**overrides):
    return replace(
        PaymentFacts(
            payment_id="payment-1",
            backend="provider",
            amount_required=Decimal("100"),
            remaining_authorization=Decimal("100"),
            status=PaymentStatus.PRE_AUTH,
        ),
        **overrides,
    )


def charge_intent(**overrides):
    return OperationIntent(
        **{
            "operation_id": "charge-1",
            "operation_type": OperationType.CHARGE,
            **overrides,
        }
    )


def test_reservation_owns_nested_parameters_and_normalizes_mapping_order():
    source = {"items": [{"price": Decimal("1.00"), "name": "a"}]}
    intent = charge_intent(parameters=source)
    record = plan_reservation(authorized_facts(), (), intent).operation
    source["items"][0]["price"] = Decimal("9")
    source["items"].append({"name": "b"})

    same = charge_intent(
        parameters={"items": [{"name": "a", "price": Decimal("1")}]}
    )
    assert intent.parameters_digest == same.parameters_digest
    assert record.parameters["items"][0]["price"] == Decimal("1")
    assert len(record.parameters["items"]) == 1
    with pytest.raises(TypeError):
        record.parameters["items"][0]["price"] = Decimal("4")
    assert record.starting_authorization == Decimal("100")
    assert record.backend == "provider"
    assert record.idempotency_key
    assert (
        plan_reservation(authorized_facts(), (record,), same).operation
        == record
    )


async def test_atomic_submission_claim_freezes_first_window_and_scope():
    repository = InMemoryDurableRepository([authorized_facts()])
    await repository.reserve_operation("payment-1", charge_intent())
    now = datetime(2026, 9, 6, tzinfo=UTC)
    until = now + timedelta(hours=1)
    claims = await asyncio.gather(
        *[
            repository.claim_submission(
                "payment-1",
                "charge-1",
                expected_attempt=0,
                now=now,
                retry_until=until,
                idempotency_scope="payment",
            )
            for _ in range(2)
        ]
    )
    assert sum(plan.granted for plan in claims) == 1
    first = await repository.get_operation("payment-1", "charge-1")
    assert first.state is OperationState.SUBMITTING
    assert first.submission_attempts == 1
    assert first.submitted_at == now
    assert first.retry_until == until
    assert first.idempotency_scope == "payment"

    await repository.record_operation_outcome(
        "payment-1", "charge-1", OperationOutcome(OperationState.UNKNOWN)
    )
    retried = await repository.claim_submission(
        "payment-1",
        "charge-1",
        expected_attempt=1,
        now=now + timedelta(minutes=1),
        retry_until=until + timedelta(days=1),
        idempotency_scope="changed",
    )
    assert retried.granted
    assert retried.operation.submission_attempts == 2
    assert retried.operation.submitted_at == now
    assert retried.operation.retry_until == until
    assert retried.operation.idempotency_scope == "payment"
    assert retried.operation.idempotency_key == first.idempotency_key
    expired = await repository.claim_submission(
        "payment-1", "charge-1", expected_attempt=2, now=until
    )
    assert not expired.granted
    assert expired.operation == retried.operation


@pytest.mark.parametrize(
    "state, amount",
    [
        (OperationState.RESERVED, None),
        (OperationState.SUBMITTING, None),
        (OperationState.SUCCEEDED, Decimal("41")),
        (OperationState.SUCCEEDED, Decimal("0")),
        (OperationState.PROVIDER_PENDING, Decimal("10")),
        (OperationState.REJECTED, Decimal("10")),
        (OperationState.UNKNOWN, Decimal("10")),
    ],
)
def test_normalized_outcomes_cannot_claim_unreserved_money(state, amount):
    facts = authorized_facts()
    operation = plan_reservation(
        facts, (), charge_intent(amount=Decimal("40"))
    ).operation
    with pytest.raises(InvalidTransitionError):
        plan_outcome(
            facts, operation, OperationOutcome(state, settled_amount=amount)
        )


@pytest.mark.parametrize(
    "late_state, late_amount, conflicting",
    [
        (OperationState.PROVIDER_PENDING, None, False),
        (OperationState.UNKNOWN, None, False),
        (OperationState.REJECTED, None, True),
        (OperationState.SUCCEEDED, Decimal("30"), True),
    ],
)
def test_late_evidence_preserves_terminal_money_and_flags_contradictions(
    late_state, late_amount, conflicting
):
    facts = authorized_facts()
    operation = plan_reservation(
        facts, (), charge_intent(amount=Decimal("40"))
    ).operation
    completed = plan_outcome(
        facts,
        operation,
        OperationOutcome(
            OperationState.SUCCEEDED, settled_amount=Decimal("20")
        ),
    )
    late = plan_outcome(
        completed.facts,
        completed.operation,
        OperationOutcome(
            late_state, settled_amount=late_amount, correlation="capture-1"
        ),
    )
    assert late.operation.state is OperationState.SUCCEEDED
    assert late.operation.correlation == "capture-1"
    assert late.facts.captured_funds == Decimal("20")
    assert late.facts.reconciliation_required is conflicting
    assert late.operation.reconciliation_required is conflicting


def test_conflicting_correlation_never_settles_another_provider_operation():
    facts = authorized_facts()
    operation = plan_reservation(
        facts, (), charge_intent(amount=Decimal("40"))
    ).operation
    pending = plan_outcome(
        facts,
        operation,
        OperationOutcome(
            OperationState.PROVIDER_PENDING, correlation="capture-1"
        ),
    )
    conflict = plan_outcome(
        pending.facts,
        pending.operation,
        OperationOutcome(OperationState.SUCCEEDED, correlation="capture-2"),
    )
    assert conflict.operation.correlation == "capture-1"
    assert conflict.operation.state is OperationState.PROVIDER_PENDING
    assert conflict.facts.captured_funds == Decimal("0")
    assert conflict.operation.reconciliation_required
    assert conflict.facts.reconciliation_required


async def test_pending_prepare_persists_handle_and_late_success_preserves_callback():
    from getpaid_core.enums import PaymentEvent
    from getpaid_core.types import PaymentUpdate

    facts = PaymentFacts(payment_id="payment-1", amount_required=Decimal("100"))
    repository = InMemoryDurableRepository([facts])
    await repository.reserve_operation(
        "payment-1", OperationIntent("prepare-1", OperationType.PREPARE)
    )
    pending = await repository.record_operation_outcome(
        "payment-1",
        "prepare-1",
        OperationOutcome(
            OperationState.PROVIDER_PENDING, external_id="order-1"
        ),
    )
    assert pending.facts.external_id == "order-1"
    await repository.apply_observation(
        "payment-1",
        PaymentUpdate(
            payment_event=PaymentEvent.LOCKED,
            locked_amount=Decimal("100"),
            external_id="order-1",
        ),
    )
    success = await repository.record_operation_outcome(
        "payment-1",
        "prepare-1",
        OperationOutcome(OperationState.SUCCEEDED, external_id="order-1"),
    )
    assert success.facts.status is PaymentStatus.PRE_AUTH
    assert success.facts.external_id == "order-1"
    conflict = await repository.record_operation_outcome(
        "payment-1",
        "prepare-1",
        OperationOutcome(OperationState.SUCCEEDED, external_id="order-2"),
    )
    assert conflict.facts.external_id == "order-1"
    assert conflict.facts.reconciliation_required


@pytest.mark.parametrize(
    "state, correlation, allowed",
    [
        (OperationState.RESERVED, "refund-handle", False),
        (OperationState.SUBMITTING, "refund-handle", False),
        (OperationState.UNKNOWN, "refund-handle", False),
        (OperationState.PROVIDER_PENDING, None, False),
        (OperationState.PROVIDER_PENDING, "", False),
        (OperationState.PROVIDER_PENDING, "refund-handle", True),
    ],
)
def test_cancellation_only_reserves_correlated_provider_pending_refund(
    state, correlation, allowed
):
    facts = authorized_facts(
        captured_funds=Decimal("100"),
        remaining_authorization=Decimal("0"),
        status=PaymentStatus.PAID,
    )
    refund = plan_reservation(
        facts, (), OperationIntent("refund-1", OperationType.START_REFUND)
    ).operation
    refund = replace(refund, state=state, correlation=correlation)
    intent = OperationIntent(
        "cancel-1",
        OperationType.CANCEL_REFUND,
        parameters={CANCELLATION_TARGET: "refund-1"},
    )
    if not allowed:
        with pytest.raises(OperationConflictError):
            plan_reservation(facts, (refund,), intent)
        return
    cancellation = plan_reservation(facts, (refund,), intent).operation
    assert cancellation.parameters[CANCELLATION_TARGET] == "refund-1"
    assert cancellation.parameters["target_correlation"] == "refund-handle"
    assert cancellation.parameters_digest == intent.parameters_digest
    assert (
        plan_reservation(facts, (refund, cancellation), intent).operation
        == cancellation
    )


@pytest.mark.parametrize("refund_wins", [False, True])
async def test_cancellation_atomically_resolves_target_without_erasing_refund(
    refund_wins,
):
    facts = authorized_facts(
        captured_funds=Decimal("100"),
        remaining_authorization=Decimal("0"),
        status=PaymentStatus.PAID,
    )
    repository = InMemoryDurableRepository([facts])
    await repository.reserve_operation(
        "payment-1", OperationIntent("refund-1", OperationType.START_REFUND)
    )
    await repository.record_operation_outcome(
        "payment-1",
        "refund-1",
        OperationOutcome(
            OperationState.PROVIDER_PENDING, correlation="provider-refund"
        ),
    )
    await repository.reserve_operation(
        "payment-1",
        OperationIntent(
            "cancel-1",
            OperationType.CANCEL_REFUND,
            parameters={CANCELLATION_TARGET: "refund-1"},
        ),
    )
    if refund_wins:
        await repository.record_operation_outcome(
            "payment-1", "refund-1", OperationOutcome(OperationState.SUCCEEDED)
        )
    cancelled = await repository.record_operation_outcome(
        "payment-1", "cancel-1", OperationOutcome(OperationState.SUCCEEDED)
    )
    target = await repository.get_operation("payment-1", "refund-1")
    assert target.state is (
        OperationState.SUCCEEDED if refund_wins else OperationState.REJECTED
    )
    assert cancelled.facts.refunded_funds == (
        Decimal("100") if refund_wins else Decimal("0")
    )
    assert cancelled.facts.captured_funds == Decimal("100")
    assert cancelled.facts.status is (
        PaymentStatus.REFUNDED if refund_wins else PaymentStatus.PAID
    )
    assert await repository.list_unresolved_operations() == ()
    assert cancelled.operation.state is OperationState.SUCCEEDED
    assert await repository.get_payment_facts("payment-1") == cancelled.facts


async def test_unresolved_refund_status_starts_at_reservation_and_pending_cannot_regress():
    facts = authorized_facts(
        captured_funds=Decimal("100"),
        remaining_authorization=Decimal("0"),
        status=PaymentStatus.PAID,
    )
    repository = InMemoryDurableRepository([facts])
    await repository.reserve_operation(
        "payment-1", OperationIntent("refund-1", OperationType.START_REFUND)
    )
    assert (
        await repository.get_payment_facts("payment-1")
    ).status is PaymentStatus.REFUND_STARTED
    await repository.claim_submission(
        "payment-1",
        "refund-1",
        expected_attempt=0,
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert (
        await repository.get_payment_facts("payment-1")
    ).status is PaymentStatus.REFUND_STARTED
    unknown = await repository.record_operation_outcome(
        "payment-1", "refund-1", OperationOutcome(OperationState.UNKNOWN)
    )
    assert unknown.facts.status is PaymentStatus.REFUND_STARTED
    await repository.record_operation_outcome(
        "payment-1",
        "refund-1",
        OperationOutcome(OperationState.PROVIDER_PENDING),
    )
    later_unknown = await repository.record_operation_outcome(
        "payment-1", "refund-1", OperationOutcome(OperationState.UNKNOWN)
    )
    assert later_unknown.operation.state is OperationState.PROVIDER_PENDING
    assert later_unknown.facts.status is PaymentStatus.REFUND_STARTED
    assert later_unknown.facts.refunded_funds == Decimal("0")
    assert later_unknown.facts.captured_funds == Decimal("100")


def test_release_response_after_callback_preserves_confirmed_release():
    facts = authorized_facts()
    operation = plan_reservation(
        facts, (), OperationIntent("release-1", OperationType.RELEASE_LOCK)
    ).operation
    callback_facts = replace(
        facts,
        remaining_authorization=Decimal("0"),
        status=PaymentStatus.CANCELLED,
    )
    response = plan_outcome(
        callback_facts, operation, OperationOutcome(OperationState.SUCCEEDED)
    )
    assert response.operation.state is OperationState.SUCCEEDED
    assert response.facts == callback_facts
