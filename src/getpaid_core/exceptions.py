"""Exception hierarchy for payment processing."""


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
