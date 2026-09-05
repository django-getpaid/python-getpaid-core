"""Core-owned logical records for durable money operations.

These are framework-neutral value types, not storage. They describe *what*
an adapter must be able to commit atomically; the adapter owns *how* (see
:mod:`getpaid_core.durable.repository` and ADR 0001, sections 1-3 and 5).

The financial vocabulary follows ``CONTEXT.md``: captured funds, refunded
funds and remaining authorization are separate facts, and an operation
intent is distinct from the attempts made to submit it.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.types import PaymentUpdate


_EMPTY: Mapping[str, Any] = MappingProxyType({})


def _freeze(mapping: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return _EMPTY if not mapping else MappingProxyType(dict(mapping))


def _canonical_amount(amount: Decimal | None) -> str:
    """Render an amount so ``100`` and ``100.00`` compare as one value."""
    if amount is None:
        return ""
    return format(amount.normalize(), "f")


class OperationType(StrEnum):
    """The money-moving operations covered by the durable contract."""

    PREPARE = "prepare"
    CHARGE = "charge"
    RELEASE_LOCK = "release_lock"
    START_REFUND = "start_refund"
    CANCEL_REFUND = "cancel_refund"


class OperationState(StrEnum):
    """Lifecycle of one operation intent (ADR 0001, section 3).

    ``UNKNOWN`` is nonterminal: it means the provider effect could not be
    established from available evidence, never that the operation was
    rejected or that resubmission is safe.
    """

    RESERVED = "reserved"
    SUBMITTING = "submitting"
    PROVIDER_PENDING = "provider_pending"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


#: States in which an operation still holds the payment: no unrelated
#: command may be reserved while one of these is outstanding.
ACTIVE_OPERATION_STATES: frozenset[OperationState] = frozenset(
    {
        OperationState.RESERVED,
        OperationState.SUBMITTING,
        OperationState.PROVIDER_PENDING,
        OperationState.UNKNOWN,
    }
)

#: States from which no further transition is accepted.
TERMINAL_OPERATION_STATES: frozenset[OperationState] = frozenset(
    {OperationState.SUCCEEDED, OperationState.REJECTED}
)


@dataclass(frozen=True, slots=True)
class PaymentFacts:
    """The durable financial facts of one payment, addressed by identity.

    This is the *logical* payment core reasons about. It carries no
    caller-owned model instance: an adapter reads current facts, core
    plans a transition against them, and the adapter commits the result.

    ``reconciliation_required`` is recorded independently of the money:
    evidence that could not be applied consistently is discoverable from
    stored state rather than from an exception, a log line or whatever
    object happened to be in the caller's hands.
    """

    payment_id: str
    amount_required: Decimal
    captured_funds: Decimal = Decimal("0")
    refunded_funds: Decimal = Decimal("0")
    remaining_authorization: Decimal = Decimal("0")
    status: str = PaymentStatus.NEW
    external_id: str | None = None
    fraud_status: str = FraudStatus.UNKNOWN
    fraud_message: str = ""
    reconciliation_required: bool = False
    provider_data: Mapping[str, Any] = field(default=_EMPTY)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_data", _freeze(self.provider_data))


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    """Trusted evidence that one provider observation was applied.

    Replay evidence is core-owned and lives outside the unrestricted
    ``provider_data`` mapping, so processor metadata can neither seed nor
    erase it. Identity is scoped to its payment, and reuse is detected by
    normalized semantic content rather than raw-payload byte equality.
    """

    payment_id: str
    event_identity: str
    content_digest: str

    @classmethod
    def for_observation(
        cls, payment_id: str, update: PaymentUpdate
    ) -> "ReplayRecord":
        """Build the record a given observation would commit."""
        if not update.provider_event_id:
            raise InvalidTransitionError(
                "A replay record requires a provider event identity."
            )
        return cls(
            payment_id=payment_id,
            event_identity=update.provider_event_id,
            content_digest=observation_digest(update),
        )


def observation_digest(update: PaymentUpdate) -> str:
    """Digest the semantic content of an observation.

    Only core-owned semantic fields take part: the provider payload in
    ``provider_data`` is deliberately excluded, so a retransmission that
    differs only in transport noise still reads as the same observation.
    """
    parts = (
        str(update.payment_event or ""),
        str(update.fraud_event or ""),
        _canonical_amount(update.paid_amount),
        _canonical_amount(update.refunded_amount),
        _canonical_amount(update.locked_amount),
        update.external_id or "",
        update.fraud_message or "",
    )
    return sha256("\x1f".join(parts).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationIntent:
    """One deliberate instruction to a provider for a payment.

    ``operation_id`` is supplied by the application and stable across
    retries and restarts. ``parameters`` are the immutable normalized
    request parameters bound to that identity: the same identity with
    different parameters is a conflict, not a retry.
    """

    operation_id: str
    operation_type: OperationType
    amount: Decimal | None = None
    parameters: Mapping[str, Any] = field(default=_EMPTY)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_type", OperationType(self.operation_type)
        )
        object.__setattr__(self, "parameters", _freeze(self.parameters))

    @property
    def parameters_digest(self) -> str:
        """Digest binding the identity to its request semantics."""
        items = sorted(
            (key, _canonical_amount(value))
            if isinstance(value, Decimal)
            else (key, str(value))
            for key, value in self.parameters.items()
        )
        parts = [
            str(self.operation_type),
            _canonical_amount(self.amount),
            *(f"{key}={value}" for key, value in items),
        ]
        return sha256("\x1f".join(parts).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """The durable reservation and current state of one operation intent.

    ``starting_captured``/``starting_refunded`` freeze the totals the
    reservation resolved against, so a settlement is derived from the
    reserved intent rather than from whatever the payment looks like when
    the response arrives.
    """

    payment_id: str
    operation_id: str
    operation_type: OperationType
    state: OperationState
    resolved_amount: Decimal | None
    parameters_digest: str
    starting_captured: Decimal
    starting_refunded: Decimal
    correlation: str | None = None
    reconciliation_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_type", OperationType(self.operation_type)
        )
        object.__setattr__(self, "state", OperationState(self.state))

    @property
    def is_active(self) -> bool:
        """Whether this operation still holds the payment."""
        return self.state in ACTIVE_OPERATION_STATES


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    """Normalized evidence about what happened to an operation.

    ``correlation`` is the safe provider handle for the operation, kept
    per operation rather than overwriting a single payment-wide id.
    """

    state: OperationState
    settled_amount: Decimal | None = None
    correlation: str | None = None
    reconciliation_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", OperationState(self.state))


@dataclass(frozen=True, slots=True)
class ObservationPlan:
    """What an adapter must commit for one provider observation.

    ``facts`` and ``replay_record`` commit together or not at all. When
    ``applied`` is false the observation added no new financial evidence,
    but ``facts`` may still differ from what was read -- a reused event
    identity carrying different content sets
    ``facts.reconciliation_required`` -- so the adapter commits ``facts``
    either way.
    """

    facts: PaymentFacts
    replay_record: ReplayRecord | None
    applied: bool


@dataclass(frozen=True, slots=True)
class ReservationPlan:
    """What an adapter must commit to reserve an operation intent.

    ``created`` is false when the same operation identity and parameters
    were already reserved: the caller resumes that reservation instead of
    starting a second one.
    """

    operation: OperationRecord
    created: bool


@dataclass(frozen=True, slots=True)
class OutcomePlan:
    """What an adapter must commit when an operation resolves.

    The operation record and the financial facts it settles commit
    together, so terminal evidence and its money never diverge.
    """

    operation: OperationRecord
    facts: PaymentFacts
