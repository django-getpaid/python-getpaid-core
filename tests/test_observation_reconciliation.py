"""Cross-channel cumulative evidence, against current durable facts."""

from decimal import Decimal as D

import pytest

from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable.records import PaymentObservation
from getpaid_core.durable import OperationType
from getpaid_core.durable import PaymentFacts
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import ReconciliationBlockedError
from getpaid_core.types import PaymentUpdate


@pytest.mark.parametrize(
    "status,refunded",
    [
        (PaymentStatus.REFUND_STARTED, "0"),
        (PaymentStatus.PARTIALLY_REFUNDED, "30"),
        (PaymentStatus.REFUNDED, "100"),
    ],
)
@pytest.mark.parametrize("captured", ["80", "100", "120"])
@pytest.mark.parametrize("identity", [None, "callback", "pull"])
async def test_capture_snapshots_preserve_refund_progress(
    status, refunded, captured, identity
):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                D("150"),
                captured_funds=D("100"),
                refunded_funds=D(refunded),
                status=status,
            )
        ]
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=D(captured),
            provider_event_id=identity,
            external_id="provider-payment",
            provider_data={"channel": "latest"},
        ),
    )
    assert plan.facts.captured_funds == D("120" if captured == "120" else "100")
    assert plan.facts.refunded_funds == D(refunded)
    expected = (
        PaymentStatus.PARTIALLY_REFUNDED
        if status == PaymentStatus.REFUNDED and captured == "120"
        else status
    )
    assert plan.facts.status == expected
    assert plan.facts.external_id == "provider-payment"
    assert plan.facts.provider_data["channel"] == "latest"
    assert plan.facts.reconciliation_required is (captured == "120")
    if captured == "120":
        with pytest.raises(ReconciliationBlockedError):
            await repository.reserve_operation(
                "payment",
                OperationIntent("next", OperationType.START_REFUND, D("10")),
            )
    replay = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=D(captured),
            provider_event_id=identity,
            external_id="provider-payment",
            provider_data={"channel": "latest"},
        ),
    )
    assert replay.facts == plan.facts
    assert replay.applied is (identity is None)


async def test_correlated_callback_completes_before_late_acceptance():
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                D("100"),
                backend="test",
                remaining_authorization=D("100"),
                status=PaymentStatus.PRE_AUTH,
            )
        ]
    )
    await repository.reserve_operation(
        "payment", OperationIntent("charge", OperationType.CHARGE, D("40"))
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentObservation(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=D("40"),
            operation_id="charge",
            outcome=OperationOutcome(
                OperationState.SUCCEEDED,
                settled_amount=D("40"),
                correlation="provider-charge",
            ),
            provider_event_id="callback",
        ),
    )
    assert plan.operations[0].state == OperationState.SUCCEEDED
    assert (
        await repository.get_operation("payment", "charge")
    ).state == OperationState.SUCCEEDED
    late = await repository.record_operation_outcome(
        "payment",
        "charge",
        OperationOutcome(
            OperationState.PROVIDER_PENDING, correlation="provider-charge"
        ),
    )
    assert late.operation.state == OperationState.SUCCEEDED
    assert late.facts.captured_funds == D("40")
    replay = await repository.apply_observation(
        "payment",
        PaymentObservation(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=D("40"),
            operation_id="charge",
            outcome=OperationOutcome(
                OperationState.SUCCEEDED,
                settled_amount=D("40"),
                correlation="provider-charge",
            ),
            provider_event_id="callback",
        ),
    )
    assert not replay.applied


@pytest.mark.parametrize("amount", ["151", "-1"])
async def test_impossible_money_is_retained_without_changing_facts(amount):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                D("150"),
                captured_funds=D("100"),
                status=PaymentStatus.PAID,
            )
        ]
    )
    update = PaymentUpdate(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=D(amount),
        provider_event_id="bad",
        provider_data={"secret": "not retained"},
    )
    plan = await repository.apply_observation("payment", update)
    assert not plan.applied
    assert plan.facts.captured_funds == D("100")
    assert plan.facts.reconciliation_required
    (evidence,) = plan.facts.observation_conflicts
    assert amount in evidence.semantic_content
    assert "secret" not in evidence.semantic_content
    assert "not retained" not in evidence.semantic_content
    assert plan.replay_record is None
    again = await repository.apply_observation("payment", update)
    assert again.facts.observation_conflicts == plan.facts.observation_conflicts


async def test_conflicting_identity_retains_both_financial_claims():
    repository = InMemoryDurableRepository(
        [PaymentFacts("payment", D("150"), status=PaymentStatus.PREPARED)]
    )
    await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=D("40"),
            provider_event_id="same",
        ),
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=D("100"),
            provider_event_id="same",
        ),
    )
    assert plan.facts.captured_funds == D("40")
    assert "100" in plan.facts.observation_conflicts[0].semantic_content


async def test_stale_capture_does_not_discard_new_refund_or_metadata():
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                D("150"),
                captured_funds=D("100"),
                refunded_funds=D("20"),
                status=PaymentStatus.PARTIALLY_REFUNDED,
            )
        ]
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=D("80"),
            refunded_amount=D("30"),
            external_id="payment-handle",
        ),
    )
    assert plan.facts.captured_funds == D("100")
    assert plan.facts.refunded_funds == D("30")
    assert plan.facts.external_id == "payment-handle"
    assert plan.facts.status == PaymentStatus.PARTIALLY_REFUNDED
