"""Exception hierarchy for payment processing."""

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from getpaid_core.types import ChargeResult


class GetPaidException(Exception):
    """Base exception for all getpaid errors."""

    def __init__(self, message: str = "", context: dict | None = None) -> None:
        super().__init__(message)
        self.context = context or {}


class CommunicationError(GetPaidException):
    """Error communicating with payment gateway."""


class ChargeFailure(CommunicationError):
    """Failed to charge payment."""


class LockFailure(CommunicationError):
    """Failed to lock (pre-authorize) payment."""


class RefundFailure(CommunicationError):
    """Failed to process refund."""


class CredentialsError(GetPaidException):
    """Invalid or missing gateway credentials."""


class InvalidCallbackError(GetPaidException):
    """Callback verification failed."""


class InvalidTransitionError(GetPaidException):
    """Attempted invalid state transition."""


class ReconciliationRequiredError(GetPaidException):
    """Gateway operation succeeded but the local update failed.

    Money moved at the payment provider without a corresponding local
    record. The gateway result is carried in :attr:`charge_result` (and
    in ``context``) so operators can reconcile the payment manually.

    That result keeps the provider metadata core deliberately leaves out
    of its logs. Treat it as sensitive recovery evidence and route it to
    a controlled channel rather than to a general-purpose logger.
    """

    def __init__(
        self,
        message: str = "",
        context: dict | None = None,
        charge_result: "ChargeResult | None" = None,
    ) -> None:
        super().__init__(message, context=context)
        self.charge_result = charge_result


class BackendNotFoundError(GetPaidException, KeyError):
    """No payment backend registered for the requested slug.

    Inherits from ``KeyError`` for backwards compatibility with callers
    that catch ``KeyError`` around registry lookups.
    """

    def __str__(self) -> str:
        # Bypass KeyError.__str__ (which repr()s the message).
        return Exception.__str__(self)
