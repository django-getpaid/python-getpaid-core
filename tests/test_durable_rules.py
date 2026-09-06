"""Core validation/transition rules applied to current durable state."""

from decimal import Decimal

import pytest

from getpaid_core.durable import PaymentFacts
from getpaid_core.durable import ReplayRecord
from getpaid_core.durable import plan_observation
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.types import PaymentUpdate


def prepared_facts(**overrides) -> PaymentFacts:
    defaults = {
        "payment_id": "pay-1",
        "amount_required": Decimal("100.00"),
        "captured_funds": Decimal("0"),
        "refunded_funds": Decimal("0"),
        "remaining_authorization": Decimal("0"),
        "status": PaymentStatus.PREPARED,
    }
    return PaymentFacts(**{**defaults, **overrides})


def capture_observation(amount: str, event_identity: str) -> PaymentUpdate:
    return PaymentUpdate(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=Decimal(amount),
        provider_event_id=event_identity,
    )


def replay_for(update: PaymentUpdate) -> tuple[ReplayRecord, ...]:
    return (ReplayRecord.for_observation("pay-1", update),)


def test_capture_observation_commits_facts_and_replay_record():
    plan = plan_observation(
        prepared_facts(), (), capture_observation("100.00", "full")
    )

    assert plan.applied is True
    assert plan.facts.captured_funds == Decimal("100.00")
    assert plan.facts.status == PaymentStatus.PAID
    assert plan.replay_record is not None
    assert plan.replay_record.event_identity == "full"
    assert plan.facts.reconciliation_required is False


def test_stale_cumulative_capture_never_regresses_committed_funds():
    committed = prepared_facts(
        captured_funds=Decimal("100.00"), status=PaymentStatus.PAID
    )
    replay = replay_for(capture_observation("100.00", "full"))

    plan = plan_observation(
        committed, replay, capture_observation("40.00", "partial")
    )

    assert plan.facts.captured_funds == Decimal("100.00")
    assert plan.facts.status == PaymentStatus.PAID
    assert plan.replay_record is not None
    assert plan.replay_record.event_identity == "partial"


def test_duplicate_event_identity_is_idempotent():
    committed = prepared_facts(
        captured_funds=Decimal("100.00"), status=PaymentStatus.PAID
    )
    replay = replay_for(capture_observation("100.00", "full"))

    plan = plan_observation(
        committed, replay, capture_observation("100.00", "full")
    )

    assert plan.applied is False
    assert plan.replay_record is None
    assert plan.facts == committed


def test_reused_event_identity_with_different_content_requires_reconciliation():
    committed = prepared_facts(
        captured_funds=Decimal("40.00"), status=PaymentStatus.PARTIAL
    )
    replay = replay_for(capture_observation("40.00", "e-1"))

    plan = plan_observation(
        committed, replay, capture_observation("100.00", "e-1")
    )

    assert plan.applied is False
    assert plan.facts.reconciliation_required is True
    assert plan.facts.captured_funds == Decimal("40.00")


def test_impossible_transition_is_an_error_not_a_silent_skip():
    committed = prepared_facts(
        captured_funds=Decimal("100.00"),
        refunded_funds=Decimal("100.00"),
        status=PaymentStatus.REFUNDED,
    )

    with pytest.raises(InvalidTransitionError, match="capture"):
        plan_observation(committed, (), capture_observation("100.00", "late"))
