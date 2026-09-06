"""Financial update validation and atomic rejection at the public FSM seam."""

from copy import deepcopy
from decimal import Decimal

import pytest

from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.fsm import apply_payment_update
from getpaid_core.types import PaymentUpdate
from tests.conftest import MockPayment


@pytest.mark.parametrize(
    ("event", "field", "status", "paid"),
    [
        ("locked", "locked_amount", "prepared", "0"),
        ("payment_captured", "paid_amount", "prepared", "0"),
        ("refund_confirmed", "refunded_amount", "refund_started", "100"),
    ],
)
@pytest.mark.parametrize(
    "amount",
    [
        Decimal(value)
        for value in ("-1", "NaN", "sNaN", "Infinity", "-Infinity", "101")
    ]
    + [1, "10"],
)
def test_invalid_amount_rolls_back_all_update_fields(
    event, field, status, paid, amount
):
    payment = MockPayment(
        status=status,
        amount_paid=Decimal(paid),
        external_id="original",
        fraud_message="unchanged",
        provider_data={"existing": {"value": 1}, "applied_event_ids": ["old"]},
    )
    before = {**vars(payment), "provider_data": deepcopy(payment.provider_data)}
    update = PaymentUpdate(
        payment_event=event,
        **{field: amount},
        external_id="replacement",
        fraud_event="accept",
        fraud_message="changed",
        provider_event_id="invalid",
        provider_data={"existing": {"value": 2}},
    )

    with pytest.raises(InvalidTransitionError):
        apply_payment_update(payment, update)

    assert {key: getattr(payment, key) for key in before} == before
    # Rejection must not consume the event ID, including its transient cache.
    apply_payment_update(payment, PaymentUpdate(provider_event_id="invalid"))
    assert payment.provider_data["applied_event_ids"] == ["old", "invalid"]


@pytest.mark.parametrize("amount", [None, Decimal("0"), Decimal("-0")])
def test_lock_requires_positive_explicit_authorization(amount):
    payment = MockPayment(status="prepared")

    with pytest.raises(InvalidTransitionError):
        apply_payment_update(
            payment, PaymentUpdate(payment_event="locked", locked_amount=amount)
        )

    assert payment.status == "prepared"
    assert payment.amount_locked == Decimal("0")


@pytest.mark.parametrize(
    "field", ["paid_amount", "refunded_amount", "locked_amount"]
)
def test_unused_financial_field_is_still_validated(field):
    payment = MockPayment(status="prepared")

    with pytest.raises(InvalidTransitionError):
        apply_payment_update(
            payment,
            PaymentUpdate(
                **{field: Decimal("NaN")}, provider_event_id="invalid"
            ),
        )

    assert payment.provider_data == {}


@pytest.mark.parametrize(
    ("event", "field", "status", "balances", "expected"),
    [
        (
            "locked",
            "locked_amount",
            "pre-auth",
            {"amount_locked": Decimal("60")},
            "pre-auth",
        ),
        (
            "payment_captured",
            "paid_amount",
            "partially_paid",
            {"amount_paid": Decimal("60"), "amount_locked": Decimal("40")},
            "partially_paid",
        ),
        (
            "refund_confirmed",
            "refunded_amount",
            "partially_refunded",
            {"amount_paid": Decimal("100"), "amount_refunded": Decimal("60")},
            "partially_refunded",
        ),
    ],
)
def test_valid_lower_snapshot_preserves_balances(
    event, field, status, balances, expected
):
    payment = MockPayment(status=status, **balances)

    apply_payment_update(
        payment,
        PaymentUpdate(
            payment_event=event,
            **{field: Decimal("20")},
            provider_event_id="stale",
        ),
    )

    assert payment.status == expected
    for name, value in balances.items():
        assert getattr(payment, name) == value
    assert payment.provider_data["applied_event_ids"] == ["stale"]


@pytest.mark.parametrize(
    ("event", "field", "status", "paid"),
    [
        ("payment_captured", "paid_amount", "prepared", "0"),
        ("refund_confirmed", "refunded_amount", "partially_paid", "100"),
    ],
)
def test_zero_cumulative_snapshot_is_valid(event, field, status, paid):
    payment = MockPayment(status=status, amount_paid=Decimal(paid))

    apply_payment_update(
        payment, PaymentUpdate(payment_event=event, **{field: Decimal("0")})
    )

    assert payment.amount_paid == Decimal(paid)
    assert payment.amount_refunded == Decimal("0")
    assert payment.status != "pre-auth"


def test_lock_is_bounded_by_uncaptured_total():
    payment = MockPayment(
        status="pre-auth",
        amount_paid=Decimal("40"),
        amount_locked=Decimal("60"),
    )

    with pytest.raises(InvalidTransitionError):
        apply_payment_update(
            payment,
            PaymentUpdate(payment_event="locked", locked_amount=Decimal("61")),
        )

    apply_payment_update(
        payment,
        PaymentUpdate(payment_event="locked", locked_amount=Decimal("60")),
    )
    assert payment.amount_locked == Decimal("60")


def test_lock_replay_after_capture_is_still_idempotent():
    payment = MockPayment(status="prepared")
    lock = PaymentUpdate(
        payment_event="locked",
        locked_amount=Decimal("100"),
        provider_event_id="lock",
    )
    apply_payment_update(payment, lock)
    apply_payment_update(
        payment,
        PaymentUpdate(
            payment_event="payment_captured", paid_amount=Decimal("100")
        ),
    )

    apply_payment_update(payment, lock)

    assert payment.status == "paid"
    assert payment.amount_locked == Decimal("0")
    assert payment.provider_data["applied_event_ids"] == ["lock"]


@pytest.mark.parametrize(
    "balances",
    [
        {"amount_paid": Decimal("101")},
        {"amount_refunded": Decimal("1")},
        {"amount_locked": Decimal("101")},
        {"amount_paid": Decimal("40"), "amount_locked": Decimal("61")},
    ],
)
def test_invalid_stored_bounds_reject_updates_without_mutation(balances):
    payment = MockPayment(status="prepared", **balances)

    with pytest.raises(InvalidTransitionError):
        apply_payment_update(
            payment,
            PaymentUpdate(
                provider_event_id="invalid", provider_data={"changed": True}
            ),
        )

    assert payment.provider_data == {}
