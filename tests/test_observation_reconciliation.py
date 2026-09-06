"""Cross-channel cumulative evidence, against current durable facts."""

from decimal import Decimal
from decimal import localcontext

import pytest

from getpaid_core.durable import InMemoryDurableRepository
from getpaid_core.durable import OperationIntent
from getpaid_core.durable import OperationOutcome
from getpaid_core.durable import OperationState
from getpaid_core.durable import OperationType
from getpaid_core.durable import PaymentFacts
from getpaid_core.durable import observation_digest
from getpaid_core.durable.records import PaymentObservation
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
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
                Decimal("150"),
                captured_funds=Decimal("100"),
                refunded_funds=Decimal(refunded),
                status=status,
            )
        ]
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal(captured),
            provider_event_id=identity,
            external_id="provider-payment",
            provider_data={"channel": "latest"},
        ),
    )
    assert plan.facts.captured_funds == Decimal(
        "120" if captured == "120" else "100"
    )
    assert plan.facts.refunded_funds == Decimal(refunded)
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
                OperationIntent(
                    "next", OperationType.START_REFUND, Decimal("10")
                ),
            )
    replay = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal(captured),
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
                Decimal("100"),
                backend="test",
                remaining_authorization=Decimal("100"),
                status=PaymentStatus.PRE_AUTH,
            )
        ]
    )
    await repository.reserve_operation(
        "payment",
        OperationIntent("charge", OperationType.CHARGE, Decimal("40")),
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentObservation(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("40"),
            operation_id="charge",
            outcome=OperationOutcome(
                OperationState.SUCCEEDED,
                settled_amount=Decimal("40"),
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
    assert late.facts.captured_funds == Decimal("40")
    replay = await repository.apply_observation(
        "payment",
        PaymentObservation(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("40"),
            operation_id="charge",
            outcome=OperationOutcome(
                OperationState.SUCCEEDED,
                settled_amount=Decimal("40"),
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
                Decimal("150"),
                captured_funds=Decimal("100"),
                status=PaymentStatus.PAID,
            )
        ]
    )
    update = PaymentUpdate(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=Decimal(amount),
        provider_event_id="bad",
        provider_data={"secret": "not retained"},
    )
    plan = await repository.apply_observation("payment", update)
    assert not plan.applied
    assert plan.facts.captured_funds == Decimal("100")
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
        [PaymentFacts("payment", Decimal("150"), status=PaymentStatus.PREPARED)]
    )
    await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("40"),
            provider_event_id="same",
        ),
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("100"),
            provider_event_id="same",
        ),
    )
    assert plan.facts.captured_funds == Decimal("40")
    assert "100" in plan.facts.observation_conflicts[0].semantic_content


async def test_stale_capture_does_not_discard_new_refund_or_metadata():
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("150"),
                captured_funds=Decimal("100"),
                refunded_funds=Decimal("20"),
                status=PaymentStatus.PARTIALLY_REFUNDED,
            )
        ]
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("80"),
            refunded_amount=Decimal("30"),
            external_id="payment-handle",
        ),
    )
    assert plan.facts.captured_funds == Decimal("100")
    assert plan.facts.refunded_funds == Decimal("30")
    assert plan.facts.external_id == "payment-handle"
    assert plan.facts.status == PaymentStatus.PARTIALLY_REFUNDED


@pytest.mark.parametrize("scoped", [False, True])
async def test_delayed_authorization_release_is_scoped_and_never_refunds(
    scoped,
):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                captured_funds=Decimal("30"),
                remaining_authorization=Decimal("70"),
                status=PaymentStatus.PARTIAL,
            )
        ]
    )
    update = PaymentObservation(
        payment_event=PaymentEvent.LOCK_RELEASED,
        cancellation_scope=OperationType.RELEASE_LOCK if scoped else None,
        paid_amount=Decimal("20"),
        provider_event_id="release",
    )
    plan = await repository.apply_observation("payment", update)
    assert plan.facts.captured_funds == Decimal("30")
    assert plan.facts.refunded_funds == 0
    assert plan.facts.remaining_authorization == Decimal(
        "0" if scoped else "70"
    )
    assert plan.facts.status == PaymentStatus.PARTIAL
    assert plan.facts.reconciliation_required is not scoped
    if scoped:
        # A differently identified repeat is still the same scoped fact.
        update.provider_event_id = "later"
        again = await repository.apply_observation("payment", update)
        assert again.facts == plan.facts


async def test_ambiguous_refund_cancellation_cannot_clear_active_refund():
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                captured_funds=Decimal("100"),
                status=PaymentStatus.PAID,
            )
        ]
    )
    await repository.reserve_operation(
        "payment",
        OperationIntent("refund", OperationType.START_REFUND, Decimal("30")),
    )
    plan = await repository.apply_observation(
        "payment", PaymentUpdate(payment_event=PaymentEvent.REFUND_CANCELLED)
    )
    assert plan.facts.status == PaymentStatus.REFUND_STARTED
    assert plan.facts.refunded_funds == 0
    assert plan.facts.reconciliation_required
    assert (await repository.get_operation("payment", "refund")).is_active


@pytest.mark.parametrize("correlated", [False, True])
async def test_delta_only_evidence_needs_proven_intent_history(correlated):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                captured_funds=Decimal("30"),
                remaining_authorization=Decimal("70"),
                status=PaymentStatus.PARTIAL,
            )
        ]
    )
    await repository.reserve_operation(
        "payment",
        OperationIntent("charge", OperationType.CHARGE, Decimal("20")),
    )
    update = PaymentObservation(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=Decimal("20"),
        delta_only=True,
        operation_id="charge" if correlated else None,
        outcome=OperationOutcome(
            OperationState.SUCCEEDED, settled_amount=Decimal("20")
        )
        if correlated
        else None,
    )
    for _ in range(2):
        plan = await repository.apply_observation("payment", update)
        assert plan.facts.captured_funds == Decimal(
            "50" if correlated else "30"
        )
        assert plan.facts.reconciliation_required is not correlated
    operation = await repository.get_operation("payment", "charge")
    assert operation.state == (
        OperationState.SUCCEEDED if correlated else OperationState.RESERVED
    )
    if not correlated:
        assert len(plan.facts.observation_conflicts) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", []),
        ("outcome", {}),
        ("delta_only", "yes"),
        ("cancellation_scope", "refund_everything"),
        ("external_id", []),
        ("fraud_message", {}),
    ],
)
async def test_malformed_observation_is_rejected_atomically(field, value):
    facts = PaymentFacts(
        "payment", Decimal("100"), status=PaymentStatus.PREPARED
    )
    repository = InMemoryDurableRepository([facts])
    update = PaymentObservation(
        payment_event=PaymentEvent.PAYMENT_CAPTURED,
        paid_amount=Decimal("40"),
        provider_event_id="event",
    )
    setattr(update, field, value)
    with pytest.raises(InvalidTransitionError):
        await repository.apply_observation("payment", update)
    assert await repository.get_payment_facts("payment") == facts
    valid = await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("40"),
            provider_event_id="event",
        ),
    )
    assert valid.applied


async def test_late_correlated_refund_acceptance_cannot_reopen_completion():
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                captured_funds=Decimal("100"),
                status=PaymentStatus.PAID,
            )
        ]
    )
    await repository.reserve_operation(
        "payment",
        OperationIntent("refund", OperationType.START_REFUND, Decimal("100")),
    )
    await repository.record_operation_outcome(
        "payment",
        "refund",
        OperationOutcome(OperationState.SUCCEEDED, correlation="refund-handle"),
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentObservation(
            payment_event=PaymentEvent.REFUND_REQUESTED,
            outcome=OperationOutcome(
                OperationState.PROVIDER_PENDING, correlation="refund-handle"
            ),
        ),
    )
    assert plan.facts.status == PaymentStatus.REFUNDED
    assert plan.operations[0].state == OperationState.SUCCEEDED


@pytest.mark.parametrize("identity", [None, "other"])
async def test_uncorrelated_same_amount_cannot_resolve_current_refund(identity):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                captured_funds=Decimal("100"),
                status=PaymentStatus.PAID,
            )
        ]
    )
    await repository.reserve_operation(
        "payment",
        OperationIntent("refund", OperationType.START_REFUND, Decimal("30")),
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentObservation(
            payment_event=PaymentEvent.REFUND_CONFIRMED,
            refunded_amount=Decimal("30"),
            operation_id=identity,
            outcome=OperationOutcome(
                OperationState.SUCCEEDED,
                settled_amount=Decimal("30"),
                correlation="unrelated",
            ),
        ),
    )
    assert plan.facts.refunded_funds == Decimal("30")
    assert plan.facts.status == PaymentStatus.REFUND_STARTED
    assert plan.facts.reconciliation_required
    assert (
        await repository.get_operation("payment", "refund")
    ).state == OperationState.RESERVED


async def test_correlated_settlement_exceeding_reservation_is_retained():
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                captured_funds=Decimal("100"),
                status=PaymentStatus.PAID,
            )
        ]
    )
    await repository.reserve_operation(
        "payment",
        OperationIntent("refund", OperationType.START_REFUND, Decimal("100")),
    )
    await repository.apply_observation(
        "payment",
        PaymentUpdate(
            payment_event=PaymentEvent.REFUND_CONFIRMED,
            refunded_amount=Decimal("100"),
        ),
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentObservation(
            operation_id="refund",
            outcome=OperationOutcome(
                OperationState.SUCCEEDED, settled_amount=Decimal("120")
            ),
        ),
    )
    assert plan.facts.refunded_funds == Decimal("100")
    assert plan.facts.reconciliation_required
    operation = await repository.get_operation("payment", "refund")
    assert operation.state == OperationState.RESERVED
    assert operation.conflicting_outcomes[0].settled_amount == Decimal("120")


async def test_conflicting_delta_and_operation_settlement_are_not_guessed():
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
    await repository.reserve_operation(
        "payment",
        OperationIntent("charge", OperationType.CHARGE, Decimal("30")),
    )
    plan = await repository.apply_observation(
        "payment",
        PaymentObservation(
            paid_amount=Decimal("20"),
            delta_only=True,
            operation_id="charge",
            outcome=OperationOutcome(
                OperationState.SUCCEEDED, settled_amount=Decimal("15")
            ),
        ),
    )
    assert plan.facts.captured_funds == 0
    assert plan.facts.reconciliation_required
    assert plan.facts.observation_conflicts
    assert (
        await repository.get_operation("payment", "charge")
    ).state == OperationState.RESERVED


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_correlated_refund_cancellation_race_preserves_returned_funds(
    cancel_first,
):
    repository = InMemoryDurableRepository(
        [
            PaymentFacts(
                "payment",
                Decimal("100"),
                captured_funds=Decimal("100"),
                status=PaymentStatus.PAID,
            )
        ]
    )
    await repository.reserve_operation(
        "payment",
        OperationIntent("refund", OperationType.START_REFUND, Decimal("30")),
    )
    await repository.record_operation_outcome(
        "payment",
        "refund",
        OperationOutcome(
            OperationState.PROVIDER_PENDING, correlation="refund-handle"
        ),
    )
    await repository.reserve_operation(
        "payment",
        OperationIntent(
            "cancel",
            OperationType.CANCEL_REFUND,
            parameters={"target_operation_id": "refund"},
        ),
    )
    cancelled = PaymentObservation(
        payment_event=PaymentEvent.REFUND_CANCELLED,
        operation_id="cancel",
        outcome=OperationOutcome(OperationState.SUCCEEDED),
    )
    settled = PaymentObservation(
        refunded_amount=Decimal("30"),
        outcome=OperationOutcome(
            OperationState.SUCCEEDED,
            settled_amount=Decimal("30"),
            correlation="refund-handle",
        ),
    )
    for update in (
        [cancelled, settled] if cancel_first else [settled, cancelled]
    ):
        await repository.apply_observation("payment", update)
    facts = await repository.get_payment_facts("payment")
    assert facts.captured_funds == Decimal("100")
    assert facts.refunded_funds == Decimal("30")
    assert facts.status == PaymentStatus.PARTIALLY_REFUNDED
    assert (
        await repository.get_operation("payment", "refund")
    ).state == OperationState.SUCCEEDED
    assert (
        await repository.get_operation("payment", "cancel")
    ).state == OperationState.SUCCEEDED


def test_empty_durable_envelope_is_semantically_the_same_observation():
    plain = PaymentUpdate(paid_amount=Decimal("40"))
    durable = PaymentObservation(paid_amount=Decimal("40.00"))
    assert observation_digest(plain) == observation_digest(durable)


def test_replay_identity_preserves_precise_amounts_under_decimal_context():
    with localcontext() as context:
        context.prec = 6
        first = observation_digest(
            PaymentUpdate(paid_amount=Decimal("0.1234567891"))
        )
        changed = observation_digest(
            PaymentUpdate(paid_amount=Decimal("0.1234567892"))
        )
        equal = observation_digest(
            PaymentUpdate(paid_amount=Decimal("0.12345678910"))
        )
    assert first != changed
    assert first == equal


async def test_correlated_outcome_does_not_hide_impossible_lifecycle_event():
    facts = PaymentFacts(
        "payment",
        Decimal("100"),
        captured_funds=Decimal("100"),
        status=PaymentStatus.PAID,
    )
    repository = InMemoryDurableRepository([facts])
    await repository.reserve_operation(
        "payment",
        OperationIntent("refund", OperationType.START_REFUND, Decimal("30")),
    )
    with pytest.raises(InvalidTransitionError, match="fail"):
        await repository.apply_observation(
            "payment",
            PaymentObservation(
                payment_event=PaymentEvent.FAILED,
                operation_id="refund",
                outcome=OperationOutcome(OperationState.PROVIDER_PENDING),
            ),
        )
    assert (
        await repository.get_payment_facts("payment")
    ).captured_funds == Decimal("100")
    assert (
        await repository.get_operation("payment", "refund")
    ).state == OperationState.RESERVED
