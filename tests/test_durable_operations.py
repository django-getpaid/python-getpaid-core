"""Reservation and outcome rules for operation intents."""

from decimal import Decimal

import pytest

from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationRecord
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import PaymentFacts
from getpaid_core.durable import plan_outcome
from getpaid_core.durable import plan_reservation
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationConflictError


def authorized_facts(**overrides) -> PaymentFacts:
    defaults = {
        "payment_id": "pay-1",
        "amount_required": Decimal("100.00"),
        "remaining_authorization": Decimal("100.00"),
        "status": PaymentStatus.PRE_AUTH,
    }
    return PaymentFacts(**{**defaults, **overrides})


def charge_intent(operation_id: str = "op-1", **overrides) -> OperationIntent:
    return OperationIntent(
        operation_id=operation_id,
        operation_type=OperationType.CHARGE,
        **overrides,
    )


def test_reservation_resolves_an_omitted_amount_against_current_facts():
    plan = plan_reservation(authorized_facts(), (), charge_intent())

    assert plan.created is True
    assert plan.operation.state is OperationState.RESERVED
    assert plan.operation.resolved_amount == Decimal("100.00")
    assert plan.operation.starting_captured == Decimal("0")


def test_same_intent_resumes_its_reservation_without_reresolving():
    facts = authorized_facts()
    reserved = plan_reservation(facts, (), charge_intent()).operation
    later = authorized_facts(
        captured_funds=Decimal("60.00"),
        remaining_authorization=Decimal("40.00"),
        status=PaymentStatus.PARTIAL,
    )

    plan = plan_reservation(later, (reserved,), charge_intent())

    assert plan.created is False
    assert plan.operation is reserved
    assert plan.operation.resolved_amount == Decimal("100.00")


def test_same_operation_id_with_changed_parameters_conflicts():
    reserved = plan_reservation(
        authorized_facts(), (), charge_intent(amount=Decimal("40.00"))
    ).operation

    with pytest.raises(OperationConflictError, match="parameters"):
        plan_reservation(
            authorized_facts(),
            (reserved,),
            charge_intent(amount=Decimal("60.00")),
        )


def test_an_outstanding_operation_blocks_an_unrelated_command():
    reserved = plan_reservation(authorized_facts(), (), charge_intent()).operation

    with pytest.raises(OperationConflictError, match="op-1"):
        plan_reservation(
            authorized_facts(), (reserved,), charge_intent("op-2")
        )


def test_refund_cancellation_may_target_an_outstanding_refund():
    paid = PaymentFacts(
        payment_id="pay-1",
        amount_required=Decimal("100.00"),
        captured_funds=Decimal("100.00"),
        status=PaymentStatus.PAID,
    )
    refund = plan_reservation(
        paid,
        (),
        OperationIntent(
            operation_id="refund-1", operation_type=OperationType.START_REFUND
        ),
    ).operation

    plan = plan_reservation(
        paid,
        (refund,),
        OperationIntent(
            operation_id="cancel-1",
            operation_type=OperationType.CANCEL_REFUND,
            parameters={"target_operation_id": "refund-1"},
        ),
    )

    assert plan.created is True
    assert plan.operation.resolved_amount is None


def test_refund_cancellation_must_name_an_outstanding_refund():
    paid = PaymentFacts(
        payment_id="pay-1",
        amount_required=Decimal("100.00"),
        captured_funds=Decimal("100.00"),
        status=PaymentStatus.PAID,
    )

    with pytest.raises(OperationConflictError, match="target_operation_id"):
        plan_reservation(
            paid,
            (),
            OperationIntent(
                operation_id="cancel-1",
                operation_type=OperationType.CANCEL_REFUND,
            ),
        )


def test_reservation_rejects_a_charge_beyond_remaining_authorization():
    facts = authorized_facts(remaining_authorization=Decimal("40.00"))

    with pytest.raises(InvalidTransitionError, match="exceeds"):
        plan_reservation(facts, (), charge_intent(amount=Decimal("60.00")))


def test_successful_charge_settles_from_the_reserved_starting_totals():
    facts = authorized_facts()
    reserved = plan_reservation(facts, (), charge_intent()).operation

    plan = plan_outcome(
        facts, reserved, OperationOutcome(state=OperationState.SUCCEEDED)
    )

    assert plan.operation.state is OperationState.SUCCEEDED
    assert plan.facts.captured_funds == Decimal("100.00")
    assert plan.facts.status == PaymentStatus.PAID


def test_a_callback_that_already_settled_is_not_counted_twice():
    facts = authorized_facts()
    reserved = plan_reservation(facts, (), charge_intent()).operation
    already_settled = authorized_facts(
        captured_funds=Decimal("100.00"),
        remaining_authorization=Decimal("0"),
        status=PaymentStatus.PAID,
    )

    plan = plan_outcome(
        already_settled,
        reserved,
        OperationOutcome(state=OperationState.SUCCEEDED),
    )

    assert plan.facts.captured_funds == Decimal("100.00")


def test_unknown_outcome_keeps_the_operation_unresolved():
    facts = authorized_facts()
    reserved = plan_reservation(facts, (), charge_intent()).operation

    plan = plan_outcome(
        facts, reserved, OperationOutcome(state=OperationState.UNKNOWN)
    )

    assert plan.operation.state is OperationState.UNKNOWN
    assert plan.operation.is_active is True
    assert plan.facts == facts


def test_a_settled_operation_cannot_be_resettled():
    facts = authorized_facts()
    reserved = plan_reservation(facts, (), charge_intent()).operation
    settled = plan_outcome(
        facts, reserved, OperationOutcome(state=OperationState.SUCCEEDED)
    )

    with pytest.raises(InvalidTransitionError, match="succeeded"):
        plan_outcome(
            settled.facts,
            settled.operation,
            OperationOutcome(state=OperationState.REJECTED),
        )


def test_repeating_a_terminal_outcome_is_idempotent():
    facts = authorized_facts()
    reserved = plan_reservation(facts, (), charge_intent()).operation
    settled = plan_outcome(
        facts, reserved, OperationOutcome(state=OperationState.SUCCEEDED)
    )

    repeated = plan_outcome(
        settled.facts,
        settled.operation,
        OperationOutcome(state=OperationState.SUCCEEDED),
    )

    assert repeated.facts == settled.facts
    assert repeated.operation.state is OperationState.SUCCEEDED


def test_reconciliation_requirement_is_recorded_on_the_operation():
    facts = authorized_facts()
    reserved = plan_reservation(facts, (), charge_intent()).operation

    plan = plan_outcome(
        facts,
        reserved,
        OperationOutcome(
            state=OperationState.UNKNOWN,
            correlation="prov-1",
            reconciliation_required=True,
        ),
    )

    assert plan.operation.reconciliation_required is True
    assert plan.operation.correlation == "prov-1"


def test_an_active_operation_record_reports_itself_as_holding_the_payment():
    record = OperationRecord(
        payment_id="pay-1",
        operation_id="op-1",
        operation_type=OperationType.CHARGE,
        state=OperationState.PROVIDER_PENDING,
        resolved_amount=Decimal("10.00"),
        parameters_digest="",
        starting_captured=Decimal("0"),
        starting_refunded=Decimal("0"),
    )

    assert record.is_active is True
