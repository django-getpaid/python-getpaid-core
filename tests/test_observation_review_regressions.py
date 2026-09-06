"""Counterexamples found by the independent spec review of reconciliation."""

from dataclasses import replace
from decimal import Decimal

import pytest

from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import PaymentFacts
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.types import PaymentUpdate


async def test_stale_refund_total_does_not_resolve_external_refund_progress():
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("150"),
                captured_funds=Decimal("100"),
                status=PaymentStatus.PARTIAL,
            )
        ]
    )
    await repository.apply_observation(
        "payment", PaymentUpdate(payment_event=PaymentEvent.REFUND_REQUESTED)
    )
    snapshot = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("80"),
            refunded_amount=Decimal("0"),
        ),
    )
    assert snapshot.facts.status == PaymentStatus.REFUND_STARTED
    increased = await repository.apply_observation(
        "payment", PaymentUpdate(paid_amount=Decimal("120"))
    )
    assert increased.facts.status == PaymentStatus.REFUND_STARTED
    assert increased.facts.reconciliation_required


async def test_zero_cumulative_totals_do_not_imply_a_refund():
    repository = InMemoryDurableRepository(
        [PaymentFacts("payment", Decimal("100"), status=PaymentStatus.PREPARED)]
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            paid_amount=Decimal("0"),
            refunded_amount=Decimal("0"),
            external_id="new-handle",
        ),
    )
    assert plan.facts.external_id == "new-handle"
    assert plan.facts.status == PaymentStatus.PREPARED
    assert plan.facts.captured_funds == plan.facts.refunded_funds == 0
    assert not plan.facts.reconciliation_required


@pytest.mark.parametrize("identity", ["original", "different", None])
async def test_historical_capture_with_hold_does_not_become_a_dispute(identity):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                remaining_authorization=Decimal("100"),
                status=PaymentStatus.PRE_AUTH,
            )
        ]
    )
    original = PaymentUpdate(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=Decimal("40"),
        locked_amount=Decimal("60"),
        provider_event_id="original",
    )
    await repository.apply_observation("payment", original)
    await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("100"),
        ),
    )
    await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.REFUND_CONFIRMED,
            refunded_amount=Decimal("100"),
        ),
    )
    before = await repository.get_payment_facts("payment")
    plan = await repository.apply_observation(
        "payment",
        replace(original, provider_event_id=identity, external_id=None),
    )
    assert plan.facts == before
    assert not plan.facts.reconciliation_required
    assert plan.applied is (identity != "original")
