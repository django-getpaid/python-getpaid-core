"""Explicit processor capabilities and results for durable submissions.

Capabilities are contractual claims by a plugin, not guarantees core can verify
against a real provider. Absence of an operation in the capability map means
unsupported. No capability is inferred from a legacy processor method.
"""

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from getpaid_core.durable.records import OperationRecord
from getpaid_core.durable.records import OperationState
from getpaid_core.durable.records import PaymentFacts


class LookupSemantics(StrEnum):
    """What a provider query can establish about this specific intent."""

    UNSUPPORTED = "unsupported"
    AUTHORITATIVE = "authoritative"
    AUTHORITATIVE_INCLUDING_ABSENCE = "authoritative_including_absence"


@dataclass(frozen=True, slots=True)
class OperationCapabilities:
    """A supported operation's submission and authoritative lookup contract.

    ``idempotency_scope`` names the provider account/endpoint/key namespace.
    The plugin must document that scope and the payload equality rules. The
    finite window starts conservatively at the first durable submission claim,
    not at receipt of an acknowledgement. Core never extends it on retries.

    AUTHORITATIVE lookup must normalize ordinary 'not found' to UNKNOWN.
    INCLUDING_ABSENCE additionally permits REJECTED when the provider proves
    that this intent did not execute and cannot execute later. Neither mode
    authorizes submission without a still-valid idempotency guarantee.
    """

    idempotency_scope: str | None = None
    idempotency_window: timedelta | None = None
    lookup_semantics: LookupSemantics = LookupSemantics.UNSUPPORTED

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "lookup_semantics", LookupSemantics(self.lookup_semantics)
        )
        if (self.idempotency_scope is None) != (
            self.idempotency_window is None
        ):
            raise ValueError(
                "Idempotency requires both a key scope and a validity window."
            )
        if (
            self.idempotency_scope is not None
            and not self.idempotency_scope.strip()
        ):
            raise ValueError("Idempotency key scope must not be empty.")
        if (
            self.idempotency_window is not None
            and self.idempotency_window <= timedelta(0)
        ):
            raise ValueError("Idempotency validity window must be positive.")


@dataclass(frozen=True, slots=True)
class OperationResult:
    """A durable operation's outcome, never an implication of settlement.

    Snapshot and operation are committed values. On a duplicate read they can
    reflect separate read instants; terminal writes return one atomic plan.
    Persistence failures remain exceptions rather than fabricated snapshots.
    """

    operation: OperationRecord
    snapshot: PaymentFacts

    @property
    def operation_id(self) -> str:
        return self.operation.operation_id

    @property
    def outcome(self) -> OperationState:
        return self.operation.state

    @property
    def reconciliation_required(self) -> bool:
        return (
            self.operation.reconciliation_required
            or self.snapshot.reconciliation_required
        )
