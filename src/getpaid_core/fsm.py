"""State engine for payment and fraud lifecycle transitions."""

from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from getpaid_core._amounts import validate_amount
from getpaid_core.enums import FraudEvent
from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.protocols import Payment
from getpaid_core.types import PaymentUpdate


@dataclass(frozen=True)
class PaymentSnapshot:
    status: str
    amount_paid: Decimal
    amount_locked: Decimal
    amount_refunded: Decimal
    external_id: str | None
    fraud_status: str
    fraud_message: str
    provider_data: dict[str, Any] | None


def _ensure_provider_data(payment: Payment) -> dict:
    provider_data = getattr(payment, "provider_data", None)
    if provider_data is None:
        provider_data = {}
        payment.provider_data = provider_data
    return provider_data


def _coerce_payment_status(payment: Payment) -> PaymentStatus:
    status = payment.status or PaymentStatus.NEW
    return PaymentStatus(status)


def _coerce_fraud_status(payment: Payment) -> FraudStatus:
    fraud_status = payment.fraud_status or FraudStatus.UNKNOWN
    return FraudStatus(fraud_status)


# Transient (non-serialized) attribute caching a set view of
# provider_data["applied_event_ids"] for O(1) dedupe lookups.
_EVENT_ID_CACHE_ATTR = "_getpaid_applied_event_ids_cache"


def _applied_event_ids(payment: Payment) -> tuple[list, set]:
    """Return the applied-event-id list and an O(1) set view of it.

    The stored representation in ``provider_data`` stays a plain list
    (unchanged serialization); the set is a cache attached to the payment
    object and rebuilt whenever the list is replaced (e.g. rollback,
    reload from storage) or mutated externally.
    """
    provider_data = _ensure_provider_data(payment)
    applied = provider_data.setdefault("applied_event_ids", [])
    cache = getattr(payment, _EVENT_ID_CACHE_ATTR, None)
    if (
        cache is None
        or cache[0] is not applied
        or len(cache[1]) != len(applied)
    ):
        cache = (applied, set(applied))
        # Payment objects with __slots__ cannot hold the cache; fall
        # back to per-call set construction there.
        with suppress(AttributeError):
            setattr(payment, _EVENT_ID_CACHE_ATTR, cache)
    return applied, cache[1]


def _record_provider_event(
    payment: Payment, provider_event_id: str | None
) -> bool:
    if not provider_event_id:
        return True
    applied, applied_set = _applied_event_ids(payment)
    if provider_event_id in applied_set:
        return False
    applied.append(provider_event_id)
    applied_set.add(provider_event_id)
    return True


def _merge_provider_data(payment: Payment, provider_data: dict) -> None:
    if not provider_data:
        return
    _ensure_provider_data(payment).update(provider_data)


def _validate_update_amounts(payment: Payment, update: PaymentUpdate) -> None:
    """Check every supplied amount, even on metadata-only updates."""
    validate_amount(payment.amount_required, "amount_required")
    validate_amount(payment.amount_paid, "amount_paid")
    validate_amount(payment.amount_locked, "amount_locked")
    validate_amount(payment.amount_refunded, "amount_refunded")
    if update.paid_amount is not None:
        validate_amount(
            update.paid_amount,
            "Paid amount",
            maximum=payment.amount_required,
            maximum_name="amount_required",
        )
    if update.refunded_amount is not None:
        validate_amount(
            update.refunded_amount,
            "Refunded amount",
            maximum=payment.amount_paid,
            maximum_name="amount_paid",
        )
    if update.locked_amount is not None:
        validate_amount(
            update.locked_amount,
            "Locked amount",
            allow_zero=False,
            maximum=payment.amount_required - payment.amount_paid,
            maximum_name="uncaptured amount_required",
        )


def _set_paid_amount(payment: Payment, paid_amount: Decimal) -> None:
    previous_paid = payment.amount_paid
    next_paid = max(previous_paid, paid_amount)
    increment = next_paid - previous_paid
    payment.amount_paid = next_paid
    if increment > 0 and payment.amount_locked:
        payment.amount_locked = max(
            Decimal("0.00"), payment.amount_locked - increment
        )


def _set_refunded_amount(payment: Payment, refunded_amount: Decimal) -> None:
    payment.amount_refunded = max(payment.amount_refunded, refunded_amount)


def _set_locked_amount(payment: Payment, locked_amount: Decimal) -> None:
    payment.amount_locked = max(payment.amount_locked, locked_amount)


def _active_paid_status(payment: Payment) -> PaymentStatus:
    if (
        payment.amount_paid >= payment.amount_required
        and payment.amount_paid > 0
    ):
        return PaymentStatus.PAID
    if payment.amount_paid > 0:
        return PaymentStatus.PARTIAL
    if payment.amount_locked > 0:
        return PaymentStatus.PRE_AUTH
    return PaymentStatus.PREPARED


def _snapshot_payment_state(payment: Payment) -> PaymentSnapshot:
    return PaymentSnapshot(
        status=payment.status,
        amount_paid=payment.amount_paid,
        amount_locked=payment.amount_locked,
        amount_refunded=payment.amount_refunded,
        external_id=payment.external_id,
        fraud_status=payment.fraud_status,
        fraud_message=payment.fraud_message,
        provider_data=deepcopy(getattr(payment, "provider_data", None)),
    )


def _restore_payment_state(payment: Payment, snapshot: PaymentSnapshot) -> None:
    payment.status = snapshot.status
    payment.amount_paid = snapshot.amount_paid
    payment.amount_locked = snapshot.amount_locked
    payment.amount_refunded = snapshot.amount_refunded
    payment.external_id = snapshot.external_id
    payment.fraud_status = snapshot.fraud_status
    payment.fraud_message = snapshot.fraud_message
    if snapshot.provider_data is None:
        payment.provider_data = {}
    else:
        payment.provider_data = snapshot.provider_data


def _apply_payment_event(payment: Payment, update: PaymentUpdate) -> None:
    event = update.payment_event
    if event is None:
        return

    status = _coerce_payment_status(payment)

    if event is PaymentEvent.PREPARED:
        if status is PaymentStatus.NEW:
            payment.status = PaymentStatus.PREPARED
            return
        raise InvalidTransitionError(
            f"Cannot prepare payment in {status.value!r} status."
        )

    if event is PaymentEvent.LOCKED:
        if status in {
            PaymentStatus.NEW,
            PaymentStatus.PREPARED,
            PaymentStatus.PRE_AUTH,
        }:
            if update.locked_amount is None:
                raise InvalidTransitionError(
                    "LOCKED event requires explicit locked_amount."
                )
            _set_locked_amount(payment, update.locked_amount)
            payment.status = PaymentStatus.PRE_AUTH
            return
        raise InvalidTransitionError(
            f"Cannot lock payment in {status.value!r} status."
        )

    if event is PaymentEvent.CHARGE_REQUESTED:
        if status in {PaymentStatus.PRE_AUTH, PaymentStatus.IN_CHARGE}:
            payment.status = PaymentStatus.IN_CHARGE
            return
        if status in {PaymentStatus.PARTIAL, PaymentStatus.PAID}:
            return
        raise InvalidTransitionError(
            f"Cannot request charge for payment in {status.value!r} status."
        )

    if event is PaymentEvent.PAYMENT_CAPTURED:
        if status in {PaymentStatus.REFUND_STARTED, PaymentStatus.REFUNDED}:
            raise InvalidTransitionError(
                f"Cannot capture payment in {status.value!r} status."
            )
        if update.paid_amount is None:
            raise InvalidTransitionError(
                "PAYMENT_CAPTURED event requires explicit paid_amount."
            )
        _set_paid_amount(payment, update.paid_amount)
        payment.status = _active_paid_status(payment)
        return

    if event is PaymentEvent.FAILED:
        if status is PaymentStatus.FAILED:
            return
        if payment.amount_paid > 0 or payment.amount_refunded > 0:
            raise InvalidTransitionError(
                f"Cannot fail payment in {status.value!r} status."
            )
        if status in {
            PaymentStatus.NEW,
            PaymentStatus.PREPARED,
            PaymentStatus.PRE_AUTH,
            PaymentStatus.IN_CHARGE,
        }:
            payment.status = PaymentStatus.FAILED
            return
        raise InvalidTransitionError(
            f"Cannot fail payment in {status.value!r} status."
        )

    if event is PaymentEvent.REFUND_REQUESTED:
        if status in {
            PaymentStatus.PAID,
            PaymentStatus.PARTIAL,
            PaymentStatus.REFUND_STARTED,
        }:
            payment.status = PaymentStatus.REFUND_STARTED
            return
        raise InvalidTransitionError(
            f"Cannot start refund for payment in {status.value!r} status."
        )

    if event is PaymentEvent.REFUND_CONFIRMED:
        if status not in {
            PaymentStatus.PAID,
            PaymentStatus.PARTIAL,
            PaymentStatus.REFUND_STARTED,
            PaymentStatus.REFUNDED,
        }:
            raise InvalidTransitionError(
                f"Cannot confirm refund for payment in {status.value!r} status."
            )
        if update.refunded_amount is None:
            raise InvalidTransitionError(
                "REFUND_CONFIRMED event requires explicit refunded_amount."
            )
        _set_refunded_amount(payment, update.refunded_amount)
        if (
            payment.amount_refunded >= payment.amount_paid
            and payment.amount_paid > 0
        ):
            payment.status = PaymentStatus.REFUNDED
        else:
            payment.status = PaymentStatus.PARTIAL
        return

    if event is PaymentEvent.REFUND_CANCELLED:
        if status in {
            PaymentStatus.REFUND_STARTED,
            PaymentStatus.PAID,
            PaymentStatus.PARTIAL,
        }:
            payment.status = _active_paid_status(payment)
            return
        raise InvalidTransitionError(
            f"Cannot cancel refund for payment in {status.value!r} status."
        )

    if event is PaymentEvent.LOCK_RELEASED:
        if status is PaymentStatus.PRE_AUTH:
            payment.amount_locked = Decimal("0.00")
            # Nothing was captured: the payment was cancelled, not
            # refunded. Keep REFUNDED only when money actually moved.
            if payment.amount_paid > 0:
                payment.status = PaymentStatus.REFUNDED
            else:
                payment.status = PaymentStatus.CANCELLED
            return
        raise InvalidTransitionError(
            f"Cannot release lock for payment in {status.value!r} status."
        )

    raise InvalidTransitionError(f"Unsupported payment event: {event!r}")


def _apply_fraud_event(payment: Payment, update: PaymentUpdate) -> None:
    event = update.fraud_event
    if event is None:
        return

    current = _coerce_fraud_status(payment)

    if event is FraudEvent.REVIEW:
        if current in {FraudStatus.UNKNOWN, FraudStatus.CHECK}:
            payment.fraud_status = FraudStatus.CHECK
            return
    elif event is FraudEvent.ACCEPT:
        if current in {
            FraudStatus.UNKNOWN,
            FraudStatus.CHECK,
            FraudStatus.ACCEPTED,
        }:
            payment.fraud_status = FraudStatus.ACCEPTED
            return
    elif event is FraudEvent.REJECT and current in {
        FraudStatus.UNKNOWN,
        FraudStatus.CHECK,
        FraudStatus.REJECTED,
    }:
        payment.fraud_status = FraudStatus.REJECTED
        return

    event_name = event.value if isinstance(event, FraudEvent) else str(event)
    raise InvalidTransitionError(
        "Cannot apply fraud event "
        f"{event_name!r} for fraud status {current.value!r}."
    )


def apply_payment_update(
    payment: Payment, update: PaymentUpdate | None
) -> Payment:
    """Apply a semantic payment update to a payment object."""
    if update is None:
        return payment

    _validate_update_amounts(payment, update)
    snapshot = _snapshot_payment_state(payment)

    try:
        if not _record_provider_event(payment, update.provider_event_id):
            return payment

        if update.external_id is not None:
            payment.external_id = update.external_id
        if update.fraud_message is not None:
            payment.fraud_message = update.fraud_message

        _merge_provider_data(payment, update.provider_data)
        _apply_payment_event(payment, update)
        _apply_fraud_event(payment, update)
    except Exception:
        _restore_payment_state(payment, snapshot)
        raise

    return payment
