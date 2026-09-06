"""Counterexamples found by the independent spec review of reconciliation."""

from decimal import Decimal

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
