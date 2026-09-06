"""The semantic repository contract framework adapters must implement.

Every operation here addresses a payment by identity and returns
committed state. None of them accepts a caller-owned payment object: a
framework wrapper may take a model instance for ergonomics, but only its
identity is authoritative, and its financial fields are never written
back (ADR 0001, section 1).

Each operation is one atomic boundary. Whatever the adapter does inside
it -- a transaction with a reload, a row lock, a compare-and-set retry --
the payment's financial facts, the affected operation record and the
replay evidence commit together or not at all. No such boundary may span
provider I/O: the flow calls the provider outside it and applies the
normalized result afterwards.
"""

from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from typing import cast
from typing import runtime_checkable

from getpaid_core.durable.records import ObservationPlan
from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationRecord
from getpaid_core.durable.records import OutcomePlan
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.records import SubmissionPlan
from getpaid_core.exceptions import StateConflictError
from getpaid_core.exceptions import UnsupportedRepositoryError
from getpaid_core.types import PaymentUpdate


@runtime_checkable
class DurablePaymentRepository(Protocol):
    """Mandatory storage semantics for money-moving operations."""

    async def get_payment_facts(self, payment_id: str) -> PaymentFacts:
        """Return the payment's current committed financial facts.

        Raises ``KeyError`` when no payment carries that identity.
        """
        ...

    async def reserve_operation(
        self, payment_id: str, intent: OperationIntent
    ) -> OperationRecord:
        """Reserve an operation intent against current durable facts.

        Returns the committed reservation. A repeat of the same operation
        ID with the same parameters returns the existing reservation
        rather than creating a second one; a conflicting intent raises
        ``OperationConflictError``. Commit the reservation plan's facts
        alongside its operation: reserving a refund projects its unresolved
        status, without moving funds.
        """
        ...

    async def claim_submission(
        self,
        payment_id: str,
        operation_id: str,
        *,
        expected_attempt: int,
        now: datetime,
        retry_until: datetime | None = None,
        idempotency_scope: str | None = None,
    ) -> SubmissionPlan:
        """Atomically claim a submission via core's ``plan_submission``.

        Reload current facts and operation and commit the counter/state
        together. The first claim freezes time, retry window and scope.
        Before requesting a retry, callers must reconcile and verify the
        provider's idempotency guarantee still covers this intent. A claim
        is not evidence of provider acceptance. No lock spans provider I/O.
        """
        ...

    async def apply_observation(
        self, payment_id: str, update: PaymentUpdate | None
    ) -> ObservationPlan:
        """Apply a normalized observation to current durable state.

        Load current facts, replay records and complete retained operation
        history inside the same boundary; pass history as ``operations`` to
        ``plan_observation``. Commit returned facts (including retained
        ``observation_conflicts``), replay evidence and every entry in
        ``ObservationPlan.operations`` atomically, even when ``applied`` is
        false. Never discard dispute records on subsequent writes.
        """
        ...

    async def record_operation_outcome(
        self, payment_id: str, operation_id: str, outcome: OperationOutcome
    ) -> OutcomePlan:
        """Record an operation outcome and its financial effects.

        Returns the committed operation record together with the facts it
        settled and any related operations; all commit atomically, including
        ``conflicting_outcomes`` and reconciliation flags when evidence
        contradicts established facts. Never discard that tuple on later writes.
        Load the payment's complete retained history for ``plan_outcome``
        so a cancellation can resolve its target, refund status reflects
        outstanding work, and confirmed refunds with overlapping reservation
        baselines are counted. Passing only active records is insufficient.
        """
        ...

    async def get_operation(
        self, payment_id: str, operation_id: str
    ) -> OperationRecord | None:
        """Return one committed operation record, or ``None``."""
        ...

    async def list_unresolved_operations(self) -> Sequence[OperationRecord]:
        """Return operations still holding a payment or needing work.

        This is how a restarted process discovers commands to resolve,
        without relying on an exception, a log line or a caller object.
        """
        ...

    async def list_payments_requiring_reconciliation(
        self,
    ) -> Sequence[PaymentFacts]:
        """Return payments whose facts carry a reconciliation requirement.

        Not all reconciliation work has an operation behind it: evidence
        that arrives on its own -- a provider event identity reused with
        different content, say -- flags the payment without any command
        being outstanding. A restarted process finds that work here.
        """
        ...


#: The operations an adapter must provide to be usable for money
#: movement. Everything else -- which locking strategy, which storage
#: engine, retention and archival policy -- is the adapter's choice.
MANDATORY_OPERATIONS: tuple[str, ...] = (
    "get_payment_facts",
    "reserve_operation",
    "claim_submission",
    "apply_observation",
    "record_operation_outcome",
    "get_operation",
    "list_unresolved_operations",
    "list_payments_requiring_reconciliation",
)


def missing_durable_operations(repository: object) -> tuple[str, ...]:
    """Return the mandatory operations this repository does not provide."""
    return tuple(
        name
        for name in MANDATORY_OPERATIONS
        if not callable(getattr(repository, name, None))
    )


def supports_durable_state(repository: object) -> bool:
    """Whether this repository declares the durable storage semantics."""
    return not missing_durable_operations(repository)


def require_durable_state(
    repository: object, *, operation: str
) -> DurablePaymentRepository:
    """Return the repository, or refuse the operation before submission.

    Call this before any provider I/O for a money-moving operation. An
    adapter that cannot commit atomically must fail here rather than
    fall back to reading a snapshot and saving it unconditionally.
    """
    missing = missing_durable_operations(repository)
    if missing:
        raise UnsupportedRepositoryError(
            f"Repository {type(repository).__name__!r} cannot perform "
            f"{operation!r}: the durable-state contract requires "
            f"{', '.join(missing)}.",
            context={"operation": operation, "missing_operations": missing},
        )
    return cast("DurablePaymentRepository", repository)


async def commit_semantic_transition[T](
    commit: Callable[[], Awaitable[T]], *, attempts: int = 3
) -> T:
    """Retry a *local* semantic transition that lost a durable race.

    An adapter using compare-and-set raises ``StateConflictError`` when
    another writer committed first. The answer is to replan the same
    transition against freshly read state, which is what ``commit`` must
    do on each call: read current facts, plan, commit.

    Wrap only that. A local conflict is never permission to send the
    financial command to the provider again -- resubmission is governed
    by the provider's own idempotency guarantee, and a retry loop around
    provider I/O is how one intent becomes two transactions. The last
    conflict is re-raised once ``attempts`` are spent, so an adapter that
    keeps losing surfaces rather than spinning.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1.")
    for _ in range(attempts - 1):
        try:
            return await commit()
        except StateConflictError:
            continue
    return await commit()
