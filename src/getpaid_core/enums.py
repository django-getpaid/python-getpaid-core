"""Payment processing enums."""

from enum import StrEnum


class PaymentStatus(StrEnum):
    """Internal payment status."""

    NEW = "new"
    PREPARED = "prepared"
    PRE_AUTH = "pre-auth"
    IN_CHARGE = "charge_started"
    PARTIAL = "partially_paid"
    PAID = "paid"
    FAILED = "failed"
    REFUND_STARTED = "refund_started"
    REFUNDED = "refunded"


class FraudStatus(StrEnum):
    """Fraud verification status."""

    UNKNOWN = "unknown"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    CHECK = "check"


class PaymentEvent(StrEnum):
    """Semantic payment lifecycle events reported by processors."""

    PREPARED = "prepared"
    LOCKED = "locked"
    CHARGE_REQUESTED = "charge_requested"
    PAYMENT_CAPTURED = "payment_captured"
    FAILED = "failed"
    REFUND_REQUESTED = "refund_requested"
    REFUND_CONFIRMED = "refund_confirmed"
    REFUND_CANCELLED = "refund_cancelled"
    LOCK_RELEASED = "lock_released"


class FraudEvent(StrEnum):
    """Semantic fraud review events reported by processors."""

    REVIEW = "review"
    ACCEPT = "accept"
    REJECT = "reject"


class BackendMethod(StrEnum):
    """HTTP method used to initiate payment."""

    GET = "GET"
    POST = "POST"
    REST = "REST"


class ConfirmationMethod(StrEnum):
    """How the payment gateway confirms payment status."""

    PUSH = "PUSH"
    PULL = "PULL"
