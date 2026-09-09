"""Recorded assertions through the public pure planner, not storage proof."""

from dataclasses import FrozenInstanceError
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from decimal import localcontext

import pytest

from getpaid_core.durable import PaymentFacts
from getpaid_core.durable import plan_observation
from getpaid_core.durable.records import ObservationConflict
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.recorded_money import RECORDED_MONEY_BACKEND
from getpaid_core.recorded_money import RecordedMoneyCommand
from getpaid_core.recorded_money import RecordedMoneyKind
from getpaid_core.recorded_money import RecordingCorrectionReason
from getpaid_core.recorded_money import plan_recorded_money
from getpaid_core.recorded_money import project_recorded_money
from getpaid_core.types import PaymentUpdate


NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
BUSINESS_TIME = datetime(2026, 9, 8, 10, tzinfo=UTC)


def root(**overrides):
    return PaymentFacts(
        **{
            "payment_id": "recorded-1",
            "backend": RECORDED_MONEY_BACKEND,
            "amount_required": Decimal("100"),
            **overrides,
        }
    )


def receipt(command_id="receipt-1", amount="30", **overrides):
    return RecordedMoneyCommand(
        **{
            "command_id": command_id,
            "kind": RecordedMoneyKind.RECEIPT,
            "actor": "staff-reference",
            "amount": Decimal(amount),
            "occurred_at": BUSINESS_TIME,
            **overrides,
        }
    )


def repayment(command_id="repayment-1", amount="10", **overrides):
    return receipt(
        command_id,
        amount,
        **{
            "kind": RecordedMoneyKind.REPAYMENT,
            "evidence_reference": "transfer-reference",
            **overrides,
        },
    )


def test_actual_partial_and_full_repayments_preserve_gross_receipts():
    received = plan_recorded_money(root(), (), receipt(amount="100"), now=NOW)
    partial = plan_recorded_money(
        received.facts, received.projection.records, repayment(), now=NOW
    )
    assert partial.facts.status == PaymentStatus.PARTIALLY_REFUNDED
    assert partial.facts.captured_funds == Decimal("100")
    assert partial.facts.refunded_funds == Decimal("10")
    assert partial.entry.signed_amount == Decimal("-10")
    assert partial.entry.evidence_reference == "transfer-reference"
    full = plan_recorded_money(
        partial.facts,
        partial.projection.records,
        repayment("repayment-2", "90"),
        now=NOW,
    )
    assert full.facts.status == PaymentStatus.REFUNDED
    assert full.facts.captured_funds == Decimal("100")
    assert full.facts.refunded_funds == Decimal("100")
    assert full.projection.effective_receipts == (received.entry,)
    assert full.projection.effective_repayments == (partial.entry, full.entry)
    retry = plan_recorded_money(
        full.facts,
        full.projection.records,
        full.entry.command,
        now=NOW + timedelta(days=1),
    )
    assert retry.applied is False
    assert retry.entry is full.entry


def test_repayment_requires_evidence_and_cannot_exceed_unreturned_receipts():
    with pytest.raises(InvalidTransitionError):
        repayment(evidence_reference=None)
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(root(), (), repayment(), now=NOW)
    received = plan_recorded_money(root(), (), receipt(), now=NOW)
    partial = plan_recorded_money(
        received.facts, received.projection.records, repayment(), now=NOW
    )
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(
            partial.facts,
            partial.projection.records,
            repayment("too-much", "21"),
            now=NOW,
        )
    assert partial.facts.refunded_funds == Decimal("10")
    assert len(partial.projection.records) == 2


def correction(command_id="correction-1", target="receipt-1", **overrides):
    return RecordedMoneyCommand(
        **{
            "command_id": command_id,
            "kind": RecordedMoneyKind.CORRECTION,
            "actor": "reviewer-reference",
            "target_command_id": target,
            "correction_reason": RecordingCorrectionReason.MISTAKEN_ENTRY,
            **overrides,
        }
    )


def test_receipt_correction_is_an_inverse_not_a_refund_or_deletion():
    received = plan_recorded_money(
        root(),
        (),
        receipt(amount="100", evidence_reference="bank-reference"),
        now=NOW,
    )
    corrected = plan_recorded_money(
        received.facts,
        received.projection.records,
        correction(),
        now=NOW + timedelta(days=1),
    )
    assert corrected.facts.captured_funds == Decimal("0")
    assert corrected.facts.refunded_funds == Decimal("0")
    assert corrected.facts.status == PaymentStatus.PREPARED
    assert corrected.entry.signed_amount == Decimal("-100")
    assert corrected.entry.effective_at == BUSINESS_TIME
    assert corrected.entry.evidence_reference == "bank-reference"
    assert corrected.entry.recorded_at == NOW + timedelta(days=1)
    assert corrected.projection.records == (received.entry, corrected.entry)
    assert corrected.projection.records[0] is received.entry
    assert corrected.projection.effective_receipts == ()
    assert corrected.projection.effective_repayments == ()
    assert received.facts.captured_funds == Decimal("100")
    replacement = plan_recorded_money(
        corrected.facts,
        corrected.projection.records,
        receipt("truthful", "40"),
        now=NOW + timedelta(days=2),
    )
    assert replacement.facts.captured_funds == Decimal("40")
    assert replacement.facts.status == PaymentStatus.PARTIAL
    assert replacement.projection.effective_receipts == (replacement.entry,)
    assert replacement.projection.records[:2] == corrected.projection.records


def test_money_is_exact_independently_of_the_callers_decimal_context():
    required = Decimal("10000000000000000000000000000.003")
    with localcontext() as context:
        context.prec = 4
        received = plan_recorded_money(
            root(amount_required=required),
            (),
            receipt(amount="10000000000000000000000000000.001"),
            now=NOW,
        )
        full = plan_recorded_money(
            received.facts,
            received.projection.records,
            receipt("rest", "0.002"),
            now=NOW,
        )
        assert full.facts.captured_funds == required
        repaid = plan_recorded_money(
            full.facts,
            full.projection.records,
            repayment(amount="0.001"),
            now=NOW,
        )
        corrected = plan_recorded_money(
            repaid.facts,
            repaid.projection.records,
            correction(target="repayment-1"),
            now=NOW,
        )
        assert corrected.facts.captured_funds == required
        assert corrected.facts.refunded_funds == Decimal("0")
        assert corrected.entry.signed_amount == Decimal("0.001")


def test_history_with_unfunded_repayment_prefix_is_not_adopted():
    received = plan_recorded_money(root(), (), receipt(), now=NOW)
    repaid = plan_recorded_money(
        received.facts, received.projection.records, repayment(), now=NOW
    )
    reversed_history = (repaid.entry, received.entry)
    with pytest.raises(InvalidTransitionError):
        project_recorded_money(reversed_history)
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(
            repaid.facts, reversed_history, receipt("another"), now=NOW
        )


def test_repayment_correction_preserves_false_record_and_allows_truthful_replacement():
    received = plan_recorded_money(root(), (), receipt(amount="100"), now=NOW)
    repaid = plan_recorded_money(
        received.facts,
        received.projection.records,
        repayment(amount="100"),
        now=NOW,
    )
    fixed = plan_recorded_money(
        repaid.facts,
        repaid.projection.records,
        correction(target="repayment-1"),
        now=NOW + timedelta(days=1),
    )
    assert fixed.facts.captured_funds == Decimal("100")
    assert fixed.facts.refunded_funds == Decimal("0")
    assert fixed.facts.status == PaymentStatus.PAID
    assert fixed.entry.signed_amount == Decimal("100")
    assert fixed.entry.effective_at == BUSINESS_TIME
    assert fixed.entry.evidence_reference == "transfer-reference"
    assert fixed.projection.records[:2] == repaid.projection.records
    assert fixed.projection.effective_repayments == ()
    replacement = plan_recorded_money(
        fixed.facts,
        fixed.projection.records,
        repayment("truthful-repayment", "40"),
        now=NOW + timedelta(days=2),
    )
    assert replacement.facts.refunded_funds == Decimal("40")
    assert replacement.facts.status == PaymentStatus.PARTIALLY_REFUNDED
    assert replacement.projection.effective_repayments == (replacement.entry,)


def test_correction_cannot_unfund_an_actual_repayment():
    received = plan_recorded_money(root(), (), receipt(), now=NOW)
    repaid = plan_recorded_money(
        received.facts, received.projection.records, repayment(), now=NOW
    )
    before = repaid.projection.records
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(repaid.facts, before, correction(), now=NOW)
    assert repaid.projection.records is before
    assert repaid.facts.captured_funds == Decimal("30")
    assert repaid.facts.refunded_funds == Decimal("10")
    assert repaid.projection.effective_repayments == (repaid.entry,)


def test_partial_receipt_can_be_corrected_while_other_receipts_fund_repayment():
    first = plan_recorded_money(root(), (), receipt(), now=NOW)
    second = plan_recorded_money(
        first.facts,
        first.projection.records,
        receipt("receipt-2", "70"),
        now=NOW,
    )
    repaid = plan_recorded_money(
        second.facts, second.projection.records, repayment(amount="50"), now=NOW
    )
    fixed = plan_recorded_money(
        repaid.facts, repaid.projection.records, correction(), now=NOW
    )
    assert fixed.facts.captured_funds == Decimal("70")
    assert fixed.facts.refunded_funds == Decimal("50")
    assert fixed.facts.status == PaymentStatus.PARTIALLY_REFUNDED
    assert fixed.projection.effective_receipts == (second.entry,)
    assert fixed.projection.effective_repayments == (repaid.entry,)


def test_correction_replay_and_original_replay_do_not_reapply_or_resurrect():
    received = plan_recorded_money(root(), (), receipt(), now=NOW)
    fixed = plan_recorded_money(
        received.facts, received.projection.records, correction(), now=NOW
    )
    for command, original in [
        (correction(), fixed.entry),
        (receipt(), received.entry),
    ]:
        retry = plan_recorded_money(
            fixed.facts,
            fixed.projection.records,
            command,
            now=NOW + timedelta(days=1),
        )
        assert retry.applied is False
        assert retry.entry is original
        assert retry.facts is fixed.facts
        assert retry.projection.effective_receipts == ()
    for command in [
        correction("other"),
        correction("chain", "correction-1"),
        correction("missing", "absent"),
    ]:
        with pytest.raises(InvalidTransitionError):
            plan_recorded_money(
                fixed.facts, fixed.projection.records, command, now=NOW
            )
    with pytest.raises(OperationConflictError):
        plan_recorded_money(
            fixed.facts,
            fixed.projection.records,
            correction(
                correction_reason=RecordingCorrectionReason.INCORRECT_AMOUNT
            ),
            now=NOW,
        )
    with pytest.raises(OperationConflictError):
        plan_recorded_money(
            fixed.facts,
            fixed.projection.records,
            correction(target="other"),
            now=NOW,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"actor": None},
        {"actor": ""},
        {"target_command_id": None},
        {"target_command_id": ""},
        {"target_command_id": []},
        {"correction_reason": None},
        {"correction_reason": "mistaken_entry"},
        {"correction_reason": {}},
        {"amount": Decimal("1")},
        {"occurred_at": BUSINESS_TIME},
        {"evidence_reference": "override"},
    ],
)
def test_malformed_corrections_are_refused(change):
    with pytest.raises(InvalidTransitionError):
        correction(**change)


@pytest.mark.parametrize(
    "change",
    [
        {"target_command_id": "receipt-1"},
        {"correction_reason": RecordingCorrectionReason.MISTAKEN_ENTRY},
        {"amount": None},
        {"occurred_at": None},
    ],
)
def test_movements_cannot_smuggle_correction_semantics(change):
    with pytest.raises(InvalidTransitionError):
        replace(receipt(), **change)


@pytest.mark.parametrize(
    "change",
    [
        {"recorded_at": NOW.replace(tzinfo=None)},
        {"effective_at": BUSINESS_TIME.replace(tzinfo=None)},
        {"recorded_at": BUSINESS_TIME - timedelta(seconds=1)},
        {"recorded_at": NOW + timedelta(days=1)},
        {"effective_at": BUSINESS_TIME - timedelta(seconds=1)},
        {"evidence_reference": "different"},
        {"signed_amount": Decimal("NaN")},
        {"signed_amount": Decimal("Infinity")},
        {"signed_amount": 30},
        {"payment_id": ""},
        {"command": "not-command"},
    ],
)
def test_malformed_retained_evidence_is_refused(change):
    first = plan_recorded_money(root(), (), receipt(), now=NOW)
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(
            first.facts,
            (replace(first.entry, **change),),
            receipt("next"),
            now=NOW,
        )


def test_reconciliation_evidence_is_neither_cleared_nor_overwritten_even_on_retry():
    first = plan_recorded_money(root(), (), receipt(), now=NOW)
    disputed = replace(
        first.facts,
        observation_conflicts=(ObservationConflict(None, "{}", "disputed"),),
    )
    for command in (receipt(), receipt("another")):
        with pytest.raises(InvalidTransitionError):
            plan_recorded_money(
                disputed, first.projection.records, command, now=NOW
            )
    assert disputed.observation_conflicts == (
        ObservationConflict(None, "{}", "disputed"),
    )
    assert disputed.captured_funds == Decimal("30")


def test_ordinary_provider_observations_remain_monotone_not_corrections():
    captured = plan_observation(
        root(backend="provider"),
        (),
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("100"),
        ),
    )
    repaid = plan_observation(
        captured.facts,
        (),
        PaymentUpdate(
            payment_event=PaymentEvent.REFUND_CONFIRMED,
            refunded_amount=Decimal("40"),
        ),
    )
    stale = plan_observation(
        repaid.facts,
        (),
        PaymentUpdate(paid_amount=Decimal("0"), refunded_amount=Decimal("0")),
    )
    assert stale.facts.captured_funds == Decimal("100")
    assert stale.facts.refunded_funds == Decimal("40")
    assert stale.facts.status == PaymentStatus.PARTIALLY_REFUNDED
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(stale.facts, (), correction(), now=NOW)
    invalid = plan_observation(
        repaid.facts,
        (),
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("-100"),
        ),
    )
    assert invalid.facts.captured_funds == Decimal("100")
    assert invalid.facts.refunded_funds == Decimal("40")
    assert invalid.facts.reconciliation_required is True
    assert invalid.facts.observation_conflicts


def test_a_corrected_history_cannot_hide_exceeding_required_amount():
    received = plan_recorded_money(
        root(amount_required=Decimal("200")), (), receipt(amount="150"), now=NOW
    )
    fixed = plan_recorded_money(
        received.facts, received.projection.records, correction(), now=NOW
    )
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(
            replace(fixed.facts, amount_required=Decimal("100")),
            fixed.projection.records,
            receipt("new"),
            now=NOW,
        )


def test_partial_receipt_projects_literal_totals_and_retains_evidence():
    initial = root()
    command = receipt()
    plan = plan_recorded_money(initial, (), command, now=NOW)
    projection = project_recorded_money(plan.projection.records)

    assert plan.applied is True
    assert plan.facts.captured_funds == Decimal("30")
    assert plan.facts.refunded_funds == Decimal("0")
    assert plan.facts.remaining_authorization == Decimal("0")
    assert plan.facts.status == PaymentStatus.PARTIAL
    assert projection == plan.projection
    assert projection.captured_funds == Decimal("30")
    assert projection.refunded_funds == Decimal("0")
    assert projection.effective_receipts == (plan.entry,)
    assert projection.effective_repayments == ()
    assert plan.entry.command == command
    assert plan.entry.payment_id == "recorded-1"
    assert plan.entry.signed_amount == Decimal("30")
    assert plan.entry.effective_at == BUSINESS_TIME
    assert plan.entry.recorded_at == NOW
    assert initial.captured_funds == Decimal("0")


def test_separate_equal_receipts_are_legitimate_and_can_complete_payment():
    first = plan_recorded_money(root(), (), receipt(amount="50"), now=NOW)
    second = plan_recorded_money(
        first.facts,
        first.projection.records,
        receipt("receipt-2", "50.000"),
        now=NOW,
    )
    assert second.applied is True
    assert second.facts.captured_funds == Decimal("100")
    assert second.facts.status == PaymentStatus.PAID
    assert second.projection.effective_receipts == (first.entry, second.entry)
    assert second.projection.records[0] is first.entry


def test_exact_retry_uses_normalized_semantics_not_retry_clock():
    first = plan_recorded_money(root(), (), receipt(), now=NOW)
    retry = plan_recorded_money(
        first.facts,
        first.projection.records,
        receipt(
            amount="30.000",
            occurred_at=BUSINESS_TIME.astimezone(timezone(timedelta(hours=2))),
        ),
        now=NOW + timedelta(days=1),
    )
    assert retry.applied is False
    assert retry.entry is first.entry
    assert retry.entry.recorded_at == NOW
    assert retry.facts is first.facts
    assert retry.projection == first.projection


@pytest.mark.parametrize(
    "change",
    [
        {"actor": "other-actor"},
        {"amount": Decimal("31")},
        {"note": "different"},
        {"evidence_reference": "other-reference"},
        {"occurred_at": BUSINESS_TIME - timedelta(hours=1)},
    ],
)
def test_same_key_changed_semantics_conflicts(change):
    first = plan_recorded_money(root(), (), receipt(), now=NOW)
    with pytest.raises(OperationConflictError):
        plan_recorded_money(
            first.facts, first.projection.records, receipt(**change), now=NOW
        )
    assert first.facts.captured_funds == Decimal("30")
    assert len(first.projection.records) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"command_id": ""},
        {"command_id": 3},
        {"actor": " "},
        {"actor": None},
        {"actor": "line\nbreak"},
        {"actor": "x" * 257},
        {"evidence_reference": ""},
        {"evidence_reference": {}},
        {"note": {}},
        {"note": "x" * 2001},
        {"kind": "unknown"},
        {"amount": Decimal("NaN")},
        {"amount": Decimal("sNaN")},
        {"amount": Decimal("Infinity")},
        {"amount": Decimal("-Infinity")},
        {"amount": Decimal("0")},
        {"amount": Decimal("-1")},
        {"amount": 30},
        {"amount": "30"},
        {"occurred_at": BUSINESS_TIME.replace(tzinfo=None)},
        {"occurred_at": "2026-09-08"},
        {"occurred_at": NOW + timedelta(seconds=1)},
    ],
)
def test_malformed_receipts_are_refused_without_exposing_input(change):
    initial = root()
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(initial, (), replace(receipt(), **change), now=NOW)
    assert initial == root()


@pytest.mark.parametrize("now", [NOW.replace(tzinfo=None), None, "today"])
def test_audit_clock_must_be_aware_even_on_retry(now):
    first = plan_recorded_money(root(), (), receipt(), now=NOW)
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(
            first.facts, first.projection.records, receipt(), now=now
        )


@pytest.mark.parametrize(
    "change",
    [
        {"backend": "stripe"},
        {"external_id": "provider-handle"},
        {"provider_data": {"applied_event_ids": ["event"]}},
        {"remaining_authorization": Decimal("1")},
        {"reconciliation_required": True},
        {"fraud_message": "evidence"},
        {"captured_funds": Decimal("30")},
        {"refunded_funds": Decimal("1")},
        {"amount_required": Decimal("NaN")},
        {"amount_required": Decimal("0")},
        {"status": PaymentStatus.PAID},
        {"status": PaymentStatus.REFUND_STARTED},
    ],
)
def test_unexplained_or_provider_backed_root_is_not_adopted(change):
    initial = root(**change)
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(initial, (), receipt(), now=NOW)


@pytest.mark.parametrize("argument", ["operations", "replay_records"])
def test_provider_history_is_refused_even_when_zero_money(argument):
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(
            root(), (), receipt(), now=NOW, **{argument: (object(),)}
        )


def test_history_summary_disagreement_and_wrong_root_are_refused():
    first = plan_recorded_money(root(), (), receipt(), now=NOW)
    for facts, history in [
        (
            replace(first.facts, captured_funds=Decimal("31")),
            first.projection.records,
        ),
        (root(), first.projection.records),
        (
            replace(first.facts, payment_id="other-root"),
            first.projection.records,
        ),
        (first.facts, (first.entry, first.entry)),
        (first.facts, (replace(first.entry, signed_amount=Decimal("31")),)),
    ]:
        with pytest.raises(InvalidTransitionError):
            plan_recorded_money(facts, history, receipt("receipt-2"), now=NOW)


def test_overpayment_is_refused_and_subcent_values_are_not_rounded():
    first = plan_recorded_money(root(), (), receipt(amount="99.999"), now=NOW)
    with pytest.raises(InvalidTransitionError):
        plan_recorded_money(
            first.facts,
            first.projection.records,
            receipt("over", "0.002"),
            now=NOW,
        )
    full = plan_recorded_money(
        first.facts, first.projection.records, receipt("rest", "0.001"), now=NOW
    )
    assert full.facts.captured_funds == Decimal("100")
    assert full.entry.signed_amount == Decimal("0.001")


def test_records_are_immutable_and_default_representations_are_redacted():
    command = receipt(
        actor="private-actor",
        note="private-note",
        evidence_reference="private-evidence",
    )
    plan = plan_recorded_money(root(), (), command, now=NOW)
    for value in (command, plan.entry, plan.projection, plan):
        assert "private-" not in repr(value)
    with pytest.raises(FrozenInstanceError):
        command.actor = "changed"
    with pytest.raises(FrozenInstanceError):
        plan.entry.signed_amount = Decimal("90")
