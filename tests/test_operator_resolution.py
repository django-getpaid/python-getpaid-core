"""Audited decisions cannot bypass atomicity or financial invariants."""

from dataclasses import replace
from decimal import Decimal

import pytest

from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import OperatorResolution
from getpaid_core.durable import PaymentFacts
from getpaid_core.durable import PaymentObservation
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.exceptions import StateConflictError
from tests.test_durable_dispatch import NOW
from tests.test_operation_recovery import recovery_flow


def decision(**changes):
    return OperatorResolution(
        **{
            "resolution_id": "case-1",
            "actor": "operator-1",
            "reason": "Checked provider ledger",
            "evidence_references": ("ledger-1",),
            "resolved_at": NOW,
            "outcome": OperationOutcome(OperationState.SUCCEEDED),
            **changes,
        }
    )


@pytest.mark.parametrize("operation_type", list(OperationType))
@pytest.mark.parametrize(
    "state", [OperationState.SUCCEEDED, OperationState.REJECTED]
)
async def test_resolution_covers_every_operation_without_inventing_refunded_money(
    operation_type, state
):
    repository, flow, intent, calls = await recovery_flow(
        operation_type, OperationOutcome(OperationState.UNKNOWN)
    )
    before = await flow.execute_operation("pay", intent, now=NOW)
    resolution = decision(outcome=OperationOutcome(state))
    result = await flow.resolve_operation(
        "pay",
        "intent",
        resolution,
        expected_facts=before.snapshot,
        expected_operation=before.operation,
    )
    assert result.outcome is state
    assert result.operation.resolutions == (resolution,)
    assert len(calls) == 1
    expected_capture = (
        Decimal("100")
        if operation_type
        in {OperationType.START_REFUND, OperationType.CANCEL_REFUND}
        or (
            operation_type is OperationType.CHARGE
            and state is OperationState.SUCCEEDED
        )
        else Decimal("0")
    )
    expected_refund = (
        Decimal("100")
        if operation_type is OperationType.START_REFUND
        and state is OperationState.SUCCEEDED
        else Decimal("0")
    )
    assert result.snapshot.captured_funds == expected_capture
    assert result.snapshot.refunded_funds == expected_refund
    assert (await repository.get_operation("pay", "intent")).resolutions == (
        resolution,
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"actor": ""},
        {"reason": " "},
        {"evidence_references": ()},
        {"evidence_references": "not-a-sequence-of-references"},
        {"resolved_at": NOW.replace(tzinfo=None)},
        {"outcome": OperationOutcome(OperationState.UNKNOWN)},
        {"outcome": OperationOutcome(OperationState.PROVIDER_PENDING)},
        {"clear_payment_reconciliation": "yes"},
    ],
)
def test_resolution_requires_explicit_audited_evidence(changes):
    with pytest.raises(InvalidTransitionError):
        decision(**changes)


async def test_stale_operator_decision_cannot_overwrite_a_callback():
    repository, flow, intent, _ = await recovery_flow(
        OperationType.CHARGE, OperationOutcome(OperationState.UNKNOWN)
    )
    before = await flow.execute_operation("pay", intent, now=NOW)
    callback = await repository.apply_observation(
        "pay",
        PaymentObservation(
            operation_id="intent",
            outcome=OperationOutcome(OperationState.SUCCEEDED),
            provider_event_id="capture-1",
        ),
    )
    with pytest.raises(StateConflictError):
        await flow.resolve_operation(
            "pay",
            "intent",
            decision(outcome=OperationOutcome(OperationState.REJECTED)),
            expected_facts=before.snapshot,
            expected_operation=before.operation,
        )
    assert await repository.get_payment_facts("pay") == callback.facts
    assert (await repository.get_operation("pay", "intent")).resolutions == ()


@pytest.mark.parametrize(
    "amount", [Decimal("0"), Decimal("-1"), Decimal("101"), Decimal("NaN")]
)
async def test_operator_cannot_force_impossible_money(amount):
    repository, flow, intent, _ = await recovery_flow(
        OperationType.CHARGE, OperationOutcome(OperationState.UNKNOWN)
    )
    before = await flow.execute_operation("pay", intent, now=NOW)
    with pytest.raises(InvalidTransitionError):
        await flow.resolve_operation(
            "pay",
            "intent",
            decision(
                outcome=OperationOutcome(
                    OperationState.SUCCEEDED, settled_amount=amount
                )
            ),
            expected_facts=before.snapshot,
            expected_operation=before.operation,
        )
    assert await repository.get_payment_facts("pay") == before.snapshot
    assert await repository.get_operation("pay", "intent") == before.operation


async def test_operator_corrects_partial_capture_without_replaying_settlement():
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                remaining_authorization=Decimal("100"),
                status="pre-auth",
            )
        ]
    )
    await repository.reserve_operation(
        "pay", OperationIntent("capture", OperationType.CHARGE, Decimal("30"))
    )
    original = OperationOutcome(
        OperationState.SUCCEEDED,
        settled_amount=Decimal("20"),
        correlation="capture-1",
    )
    corrected = replace(original, settled_amount=Decimal("30"))
    await repository.record_operation_outcome("pay", "capture", original)
    before = await repository.record_operation_outcome(
        "pay", "capture", corrected
    )
    assert before.facts.captured_funds == Decimal("20")
    assert before.operation.conflicting_outcomes == (corrected,)
    resolution = decision(outcome=corrected, clear_payment_reconciliation=True)
    resolved = await repository.resolve_operation(
        "pay",
        "capture",
        resolution,
        expected_facts=before.facts,
        expected_operation=before.operation,
    )
    assert resolved.operation.state is OperationState.SUCCEEDED
    assert resolved.operation.settled_amount == Decimal("30")
    assert resolved.facts.captured_funds == Decimal("30")
    assert resolved.facts.remaining_authorization == Decimal("70")
    assert resolved.facts.refunded_funds == Decimal("0")
    assert not resolved.operation.reconciliation_required
    assert not resolved.facts.reconciliation_required
    assert resolved.operation.resolutions == (resolution,)
    assert original in resolved.operation.conflicting_outcomes
    assert corrected in resolved.operation.conflicting_outcomes
    assert await repository.get_payment_facts("pay") == resolved.facts
    assert (
        await repository.get_operation("pay", "capture") == resolved.operation
    )
    retried = await repository.resolve_operation(
        "pay",
        "capture",
        resolution,
        expected_facts=before.facts,
        expected_operation=before.operation,
    )
    assert retried == resolved
    repeated = await repository.record_operation_outcome(
        "pay", "capture", corrected
    )
    assert repeated == resolved
    disputed_again = await repository.record_operation_outcome(
        "pay", "capture", original
    )
    assert disputed_again.operation.reconciliation_required
    assert disputed_again.facts.captured_funds == Decimal("30")
    assert disputed_again.facts.remaining_authorization == Decimal("70")
    assert disputed_again.operation.resolutions == (resolution,)


async def partial_settlement(operation_type):
    is_capture = operation_type is OperationType.CHARGE
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                captured_funds=Decimal("10") if is_capture else Decimal("100"),
                refunded_funds=Decimal("0") if is_capture else Decimal("10"),
                remaining_authorization=Decimal("90")
                if is_capture
                else Decimal("0"),
                status=PaymentStatus.PARTIAL
                if is_capture
                else PaymentStatus.PARTIALLY_REFUNDED,
            )
        ]
    )
    await repository.reserve_operation(
        "pay", OperationIntent("partial", operation_type, Decimal("30"))
    )
    await repository.record_operation_outcome(
        "pay",
        "partial",
        OperationOutcome(
            OperationState.SUCCEEDED, settled_amount=Decimal("20")
        ),
    )
    return repository


@pytest.mark.parametrize(
    "operation_type", [OperationType.CHARGE, OperationType.START_REFUND]
)
@pytest.mark.parametrize("observed_total", [None, Decimal("40"), Decimal("50")])
@pytest.mark.parametrize("settled_amount", [None, Decimal("30")])
async def test_correction_uses_reserved_baseline_without_double_counting_observed_money(
    operation_type, observed_total, settled_amount
):
    repository = await partial_settlement(operation_type)
    if observed_total is not None:
        await repository.apply_observation(
            "pay",
            PaymentObservation(
                **{
                    "paid_amount"
                    if operation_type is OperationType.CHARGE
                    else "refunded_amount": observed_total,
                }
            ),
        )
    facts = await repository.get_payment_facts("pay")
    operation = await repository.get_operation("pay", "partial")
    result = await repository.resolve_operation(
        "pay",
        "partial",
        decision(
            outcome=OperationOutcome(
                OperationState.SUCCEEDED, settled_amount=settled_amount
            )
        ),
        expected_facts=facts,
        expected_operation=operation,
    )
    expected_total = (
        Decimal("50") if observed_total == Decimal("50") else Decimal("40")
    )
    assert result.operation.settled_amount == Decimal("30")
    if operation_type is OperationType.CHARGE:
        assert result.facts.captured_funds == expected_total
        assert result.facts.remaining_authorization == (
            Decimal("50") if observed_total == Decimal("50") else Decimal("60")
        )
        assert result.facts.refunded_funds == Decimal("0")
    else:
        assert result.facts.captured_funds == Decimal("100")
        assert result.facts.refunded_funds == expected_total
        assert result.facts.remaining_authorization == Decimal("0")


@pytest.mark.parametrize(
    "operation_type", [OperationType.CHARGE, OperationType.START_REFUND]
)
@pytest.mark.parametrize(
    "amount",
    [
        Decimal("10"),
        Decimal("31"),
        Decimal("101"),
        Decimal("NaN"),
        Decimal("Infinity"),
        "30",
    ],
)
async def test_invalid_settlement_correction_leaves_facts_evidence_and_audit_unchanged(
    operation_type, amount
):
    repository = await partial_settlement(operation_type)
    facts = await repository.get_payment_facts("pay")
    operation = await repository.get_operation("pay", "partial")
    with pytest.raises(InvalidTransitionError):
        await repository.resolve_operation(
            "pay",
            "partial",
            decision(
                outcome=OperationOutcome(
                    OperationState.SUCCEEDED, settled_amount=amount
                )
            ),
            expected_facts=facts,
            expected_operation=operation,
        )
    assert await repository.get_payment_facts("pay") == facts
    assert await repository.get_operation("pay", "partial") == operation


@pytest.mark.parametrize(
    "operation_type", [OperationType.CHARGE, OperationType.START_REFUND]
)
@pytest.mark.parametrize(
    "later_state",
    [
        OperationState.RESERVED,
        OperationState.SUCCEEDED,
        OperationState.REJECTED,
    ],
)
async def test_settlement_correction_cannot_reuse_baseline_after_later_intents(
    operation_type, later_state
):
    repository = await partial_settlement(operation_type)
    await repository.reserve_operation(
        "pay", OperationIntent("later", operation_type, Decimal("10"))
    )
    if later_state is not OperationState.RESERVED:
        await repository.record_operation_outcome(
            "pay", "later", OperationOutcome(later_state)
        )
    facts = await repository.get_payment_facts("pay")
    operation = await repository.get_operation("pay", "partial")
    later = await repository.get_operation("pay", "later")
    with pytest.raises(InvalidTransitionError, match="later intents"):
        await repository.resolve_operation(
            "pay",
            "partial",
            decision(),
            expected_facts=facts,
            expected_operation=operation,
        )
    assert await repository.get_payment_facts("pay") == facts
    assert await repository.get_operation("pay", "partial") == operation
    assert await repository.get_operation("pay", "later") == later


async def test_refund_correction_preserves_unrelated_external_refund_progress():
    repository = await partial_settlement(OperationType.START_REFUND)
    pending = await repository.apply_observation(
        "pay", PaymentObservation(payment_event=PaymentEvent.REFUND_REQUESTED)
    )
    assert pending.facts.status == PaymentStatus.REFUND_STARTED
    operation = await repository.get_operation("pay", "partial")
    resolution = decision()
    result = await repository.resolve_operation(
        "pay",
        "partial",
        resolution,
        expected_facts=pending.facts,
        expected_operation=operation,
    )
    assert result.facts.refunded_funds == Decimal("40")
    assert result.facts.captured_funds == Decimal("100")
    assert result.facts.status == PaymentStatus.REFUND_STARTED
    assert result.operation.settled_amount == Decimal("30")
    assert result.operation.resolutions == (resolution,)
    assert await repository.get_payment_facts("pay") == result.facts


@pytest.mark.parametrize(
    "state", [OperationState.REJECTED, OperationState.SUCCEEDED]
)
@pytest.mark.parametrize("clear_payment_reconciliation", [False, True])
async def test_rejected_refund_resolution_preserves_external_progress_and_blocks_capture(
    state, clear_payment_reconciliation
):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                captured_funds=Decimal("50"),
                remaining_authorization=Decimal("50"),
                status=PaymentStatus.PARTIAL,
            )
        ]
    )
    await repository.reserve_operation(
        "pay",
        OperationIntent("old-refund", OperationType.START_REFUND, Decimal("30")),
    )
    rejected = await repository.record_operation_outcome(
        "pay", "old-refund", OperationOutcome(OperationState.REJECTED)
    )
    assert rejected.facts.status == PaymentStatus.PARTIAL
    pending = await repository.apply_observation(
        "pay", PaymentObservation(payment_event=PaymentEvent.REFUND_REQUESTED)
    )
    capture = OperationIntent("capture", OperationType.CHARGE, Decimal("10"))
    with pytest.raises(InvalidTransitionError):
        await repository.reserve_operation("pay", capture)
    resolution = decision(
        outcome=OperationOutcome(state),
        clear_payment_reconciliation=clear_payment_reconciliation,
    )
    resolved = await repository.resolve_operation(
        "pay",
        "old-refund",
        resolution,
        expected_facts=pending.facts,
        expected_operation=rejected.operation,
    )
    assert resolved.facts.status == PaymentStatus.REFUND_STARTED
    assert resolved.facts.captured_funds == Decimal("50")
    assert resolved.facts.remaining_authorization == Decimal("50")
    assert resolved.facts.refunded_funds == (
        Decimal("30") if state is OperationState.SUCCEEDED else Decimal("0")
    )
    assert not resolved.facts.reconciliation_required
    assert not resolved.operation.reconciliation_required
    assert resolved.operation.state is state
    assert resolved.operation.resolutions == (resolution,)
    with pytest.raises(InvalidTransitionError):
        await repository.reserve_operation("pay", capture)
    assert await repository.get_payment_facts("pay") == resolved.facts
    assert (
        await repository.get_operation("pay", "old-refund")
        == resolved.operation
    )
    retried = await repository.resolve_operation(
        "pay",
        "old-refund",
        resolution,
        expected_facts=pending.facts,
        expected_operation=rejected.operation,
    )
    assert retried == resolved


async def test_resolution_cannot_erase_confirmed_effects_or_reuse_audit_identity():
    repository, flow, intent, _ = await recovery_flow(
        OperationType.CHARGE, OperationOutcome(OperationState.SUCCEEDED)
    )
    before = await flow.execute_operation("pay", intent, now=NOW)
    with pytest.raises(InvalidTransitionError, match="undo"):
        await flow.resolve_operation(
            "pay",
            "intent",
            decision(outcome=OperationOutcome(OperationState.REJECTED)),
            expected_facts=before.snapshot,
            expected_operation=before.operation,
        )
    resolved = await flow.resolve_operation(
        "pay",
        "intent",
        decision(),
        expected_facts=before.snapshot,
        expected_operation=before.operation,
    )
    with pytest.raises(OperationConflictError):
        await flow.resolve_operation(
            "pay",
            "intent",
            decision(reason="Changed decision"),
            expected_facts=resolved.snapshot,
            expected_operation=resolved.operation,
        )
    assert await repository.get_operation("pay", "intent") == resolved.operation


def test_operator_acknowledgement_cannot_clear_corrupt_financial_facts():
    from getpaid_core.durable import OperationIntent
    from getpaid_core.durable import PaymentFacts
    from getpaid_core.durable import plan_outcome
    from getpaid_core.durable import plan_reservation
    from getpaid_core.durable import plan_resolution

    facts = PaymentFacts(
        "pay",
        Decimal("100"),
        remaining_authorization=Decimal("100"),
        status="pre-auth",
    )
    operation = plan_reservation(
        facts, (), OperationIntent("capture", OperationType.CHARGE)
    ).operation
    completed = plan_outcome(
        facts, operation, OperationOutcome(OperationState.SUCCEEDED)
    )
    corrupt = replace(
        completed.facts,
        refunded_funds=Decimal("101"),
        reconciliation_required=True,
    )
    with pytest.raises(InvalidTransitionError):
        plan_resolution(
            corrupt,
            completed.operation,
            decision(clear_payment_reconciliation=True),
            expected_facts=corrupt,
            expected_operation=completed.operation,
            operations=(completed.operation,),
        )


@pytest.mark.parametrize(
    "later_state", [OperationState.RESERVED, OperationState.SUCCEEDED]
)
async def test_rejected_capture_resolution_cannot_reuse_baseline_after_later_intents(
    later_state,
):
    from getpaid_core.durable import OperationIntent
    from getpaid_core.durable import PaymentFacts

    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "pay",
                Decimal("100"),
                remaining_authorization=Decimal("100"),
                status="pre-auth",
            )
        ]
    )
    await repository.reserve_operation(
        "pay", OperationIntent("a", OperationType.CHARGE, Decimal("40"))
    )
    await repository.record_operation_outcome(
        "pay", "a", OperationOutcome(OperationState.REJECTED)
    )
    await repository.reserve_operation(
        "pay", OperationIntent("b", OperationType.CHARGE, Decimal("40"))
    )
    if later_state is OperationState.SUCCEEDED:
        await repository.record_operation_outcome(
            "pay", "b", OperationOutcome(OperationState.SUCCEEDED)
        )
    before = await repository.get_payment_facts("pay")
    operation = await repository.get_operation("pay", "a")
    with pytest.raises(InvalidTransitionError, match="later"):
        await repository.resolve_operation(
            "pay",
            "a",
            decision(),
            expected_facts=before,
            expected_operation=operation,
        )
    assert await repository.get_payment_facts("pay") == before
    assert await repository.get_operation("pay", "a") == operation


async def test_failed_audit_commit_leaves_uncertainty_and_no_money():
    class Unavailable(InMemoryDurableRepository):
        async def resolve_operation(self, *args, **kwargs):
            raise OSError("audit storage unavailable")

    repository, flow, intent, _ = await recovery_flow(
        OperationType.CHARGE,
        OperationOutcome(OperationState.UNKNOWN),
        Unavailable,
    )
    before = await flow.execute_operation("pay", intent, now=NOW)
    with pytest.raises(OSError):
        await flow.resolve_operation(
            "pay",
            "intent",
            decision(),
            expected_facts=before.snapshot,
            expected_operation=before.operation,
        )
    assert await repository.get_operation("pay", "intent") == before.operation
    assert await repository.get_payment_facts("pay") == before.snapshot


async def test_resolution_keeps_disputed_claims_and_requires_explicit_payment_acknowledgement():
    repository, flow, intent, _ = await recovery_flow(
        OperationType.CHARGE,
        OperationOutcome(
            OperationState.SUCCEEDED, settled_amount=Decimal("101")
        ),
    )
    before = await flow.execute_operation("pay", intent, now=NOW)
    assert before.reconciliation_required
    resolved = await flow.resolve_operation(
        "pay",
        "intent",
        decision(),
        expected_facts=before.snapshot,
        expected_operation=before.operation,
    )
    assert (
        resolved.operation.conflicting_outcomes
        == before.operation.conflicting_outcomes
    )
    assert resolved.snapshot.reconciliation_required
    acknowledged = await flow.resolve_operation(
        "pay",
        "intent",
        decision(resolution_id="case-2", clear_payment_reconciliation=True),
        expected_facts=resolved.snapshot,
        expected_operation=resolved.operation,
    )
    assert not acknowledged.reconciliation_required
    assert len(acknowledged.operation.resolutions) == 2
    # A new contradictory callback is evidence, not suppressed by old audit.
    later = await repository.record_operation_outcome(
        "pay", "intent", OperationOutcome(OperationState.REJECTED)
    )
    assert later.operation.reconciliation_required
    assert later.facts.captured_funds == Decimal("100")
    assert later.operation.resolutions == acknowledged.operation.resolutions
