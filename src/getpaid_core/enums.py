"""Payment processing enums.

Values are kept identical to django-getpaid for backward compatibility.
"""

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


class BackendMethod(StrEnum):
    """HTTP method used to initiate payment."""

    GET = "GET"
    POST = "POST"
    REST = "REST"


class ConfirmationMethod(StrEnum):
    """How the payment gateway confirms payment status."""

    PUSH = "PUSH"
    PULL = "PULL"
