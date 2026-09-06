"""Audited decisions cannot bypass atomicity or financial invariants."""

from decimal import Decimal

import pytest

from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import OperatorResolution
from getpaid_core.durable import PaymentObservation
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
