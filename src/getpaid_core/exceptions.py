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


class UnsupportedRepositoryError(GetPaidException):
    """The configured repository cannot provide the durable contract.

    Raised before a money-moving operation reaches the provider, so an
    adapter that cannot commit financial facts, operation state and
    replay evidence atomically never submits a financial command. There
    is deliberately no compatibility fallback to unconditional saves.
    """


class UnsupportedProcessorError(GetPaidException):
    """The processor has not declared a safe durable operation contract."""


class OperationEvidenceError(InvalidTransitionError):
    """A provider response cannot establish a valid operation outcome.

    This is post-submission validation failure, not provider rejection.
    Reconcile the durable intent; never blindly retry the command.
    """

    provider_resubmission_allowed = False


class OperationPersistenceError(GetPaidException):
    """Local durability failed after provider I/O; reconcile the stored intent.

    Context carries payment/operation identity, type, safe correlation,
    allowlisted ``evidence`` and whether local recovery retention was
    acknowledged (``recovery_recorded``). The original failure is ``__cause__``;
    do not log that chain indiscriminately. The pre-submission intent remains
    discoverable even when retention fails. No provider resubmission is allowed.
    """

    provider_resubmission_allowed = False


class StateConflictError(GetPaidException):
    """A concurrent writer committed first; the plan was built on stale facts.

    This is a *local* semantic conflict, not a provider failure. The
    caller may replan the same transition against freshly read facts --
    :attr:`retry_locally` -- but must never take it as permission to send
    the financial command to the provider again
    (:attr:`provider_resubmission_allowed`). Resubmission is governed by
    the provider's own idempotency guarantee, not by this error.
    """

    retry_locally = True
    provider_resubmission_allowed = False


class OperationConflictError(GetPaidException):
    """The requested operation intent conflicts with a durable one.

    Either another mutation is still outstanding on the payment, or the
    same operation ID was reserved with different request parameters.
    Neither is retryable without a decision by the caller.
    """


class ReconciliationBlockedError(OperationConflictError):
    """The payment must be reconciled before it accepts a new command.

    Raised when a reservation is attempted against facts carrying a
    reconciliation requirement -- an ambiguous migrated record, a legacy
    operation that was already pending, or contradictory evidence core
    refused to fold into the financial state.

    It is a conflict, so callers that already handle
    ``OperationConflictError`` keep working, but the remedy differs:
    waiting does not clear it. The application must establish what
    happened and record the resolution.
    """


class ConformanceError(GetPaidException):
    """A storage adapter failed a durable-contract conformance check.

    Raised only by :mod:`getpaid_core.durable.conformance`, whose message
    names the check that failed and what it observed.
    """


class BackendNotFoundError(GetPaidException, KeyError):
    """No payment backend registered for the requested slug.

    Inherits from ``KeyError`` for backwards compatibility with callers
    that catch ``KeyError`` around registry lookups.
    """

    def __str__(self) -> str:
        # Bypass KeyError.__str__ (which repr()s the message).
        return Exception.__str__(self)
