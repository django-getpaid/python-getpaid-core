"""Recorded assertions through the public pure planner, not storage proof."""

from datetime import UTC
from datetime import datetime
from decimal import Decimal

from getpaid_core.durable import PaymentFacts
from getpaid_core.enums import PaymentStatus
from getpaid_core.recorded_money import RECORDED_MONEY_BACKEND
from getpaid_core.recorded_money import RecordedMoneyCommand
from getpaid_core.recorded_money import RecordedMoneyKind
from getpaid_core.recorded_money import plan_recorded_money
from getpaid_core.recorded_money import project_recorded_money


NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)
BUSINESS_TIME = datetime(2026, 9, 8, 10, tzinfo=UTC)


def root(**overrides):
    return PaymentFacts(**{
        "payment_id": "recorded-1",
        "backend": RECORDED_MONEY_BACKEND,
        "amount_required": Decimal("100"),
        **overrides,
    })


def receipt(command_id="receipt-1", amount="30", **overrides):
    return RecordedMoneyCommand(**{
        "command_id": command_id,
        "kind": RecordedMoneyKind.RECEIPT,
        "actor": "staff-reference",
        "amount": Decimal(amount),
        "occurred_at": BUSINESS_TIME,
        **overrides,
    })


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
