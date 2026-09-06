"""Core-owned logical records for durable money operations.

These are framework-neutral value types, not storage. They describe *what*
an adapter must be able to commit atomically; the adapter owns *how* (see
:mod:`getpaid_core.durable.repository` and ADR 0001, sections 1-3 and 5).

The financial vocabulary follows ``CONTEXT.md``: captured funds, refunded
funds and remaining authorization are separate facts, and an operation
intent is distinct from the attempts made to submit it.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.types import PaymentUpdate


#: The shared empty metadata mapping, so an absent mapping costs nothing
#: and cannot be mutated.
EMPTY_METADATA: Mapping[str, Any] = MappingProxyType({})


def freeze_metadata(mapping: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Copy a metadata mapping behind a read-only view.

    Records own their metadata: the caller's mapping is copied, so later
    edits to it do not reach committed state, and the copy is proxied, so
    nothing edits it through the record either.
    """
    return EMPTY_METADATA if not mapping else MappingProxyType(dict(mapping))


#: Metadata keys the released 3.x contract used for core-owned replay
#: bookkeeping, when history and provider metadata still shared one
#: mapping. They survive as ordinary provider metadata -- readable, and
#: preserved by migration -- but nothing here or in
#: :mod:`getpaid_core.durable.rules` ever reads them as trusted evidence.
LEGACY_REPLAY_METADATA_KEYS: frozenset[str] = frozenset({"applied_event_ids"})


def validate_provider_metadata(
    metadata: Any, *, name: str = "Provider metadata"
) -> None:
    """Reject metadata core cannot store as a serializable mapping.

    This is a shape check, not an ownership check: a payload carrying a
    :data:`LEGACY_REPLAY_METADATA_KEYS` lookalike is accepted and kept as
    plain metadata. Refusing it instead would let any provider payload
    suppress a genuine financial change -- the very failure dedicated
    replay storage exists to prevent.

    Raising before anything is planned is what makes rejection atomic:
    the adapter's boundary commits nothing, so committed funds and
    committed replay evidence both survive a malformed payload.
    """
    if not isinstance(metadata, Mapping):
        raise InvalidTransitionError(
            f"{name} must be a mapping, not {type(metadata).__name__}."
        )
    for key in metadata:
        if not isinstance(key, str):
            raise InvalidTransitionError(
                f"{name} keys must be strings; got {type(key).__name__}."
            )


def validate_event_identity(value: Any) -> str | None:
    """Return an observation's provider event identity, if it carries one.

    An absent or empty identity is not an error -- an observation without
    one simply commits no replay evidence -- but a non-string identity is
    malformed and is refused before any state is planned.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidTransitionError(
            "A provider event identity must be a string, not "
            f"{type(value).__name__}."
        )
    return value or None


def _canonical_amount(amount: Decimal | None) -> str:
    """Render an amount so ``100`` and ``100.00`` compare as one value."""
    if amount is None:
        return ""
    return format(amount.normalize(), "f")


def _canonical_parameter(value: Any) -> Any:
    """Build an unambiguous typed tree, with unordered mapping semantics."""
    if isinstance(value, Mapping):
        validate_provider_metadata(value, name="Operation parameters")
        return [
            "mapping",
            [[key, _canonical_parameter(value[key])] for key in sorted(value)],
        ]
    if isinstance(value, (list, tuple)):
        return ["sequence", [_canonical_parameter(item) for item in value]]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InvalidTransitionError("Operation parameters must be finite.")
        # Avoid normalize(): it rounds through the current Decimal context.
        rendered = format(value, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return ["decimal", "0" if value == 0 else rendered]
    if value is None or type(value) in (str, bool, int):
        return [type(value).__name__, value]
    if type(value) is float and math.isfinite(value):
        return ["float", value]
    raise InvalidTransitionError(
        f"Unsupported or nonfinite operation parameter: {type(value).__name__}."
    )


def _freeze_parameter(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_parameter(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_parameter(item) for item in value)
    return value


def freeze_parameters(parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate and recursively copy a normalized immutable request."""
    validate_provider_metadata(parameters, name="Operation parameters")
    try:
        _canonical_parameter(parameters)
        return _freeze_parameter(parameters)
    except RecursionError as exc:
        raise InvalidTransitionError(
            "Operation parameters must be finite, acyclic values."
        ) from exc


def _request_digest(value: Any) -> str:
    encoded = json.dumps(
        _canonical_parameter(value), ensure_ascii=True, separators=(",", ":")
    )
    return sha256(encoded.encode()).hexdigest()


def _validate_operation_id(operation_id: str) -> None:
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise InvalidTransitionError(
            "An operation ID must be a nonempty string."
        )


#: Parameter naming the pending refund a cancellation targets.
CANCELLATION_TARGET = "target_operation_id"
#: Provider handle frozen from the target, never trusted from caller input.
CANCELLATION_CORRELATION = "target_correlation"


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
    object happened to be in the caller's hands. While it stands, no new
    operation may be reserved against the payment.

    ``backend`` names the provider the payment moves money through. It is
    not a financial fact; it is the context a provider event identity is
    unique within, and it is read from stored facts rather than from a
    caller's object.

    ``provider_data`` is unrestricted plugin metadata. It holds no
    core-owned bookkeeping: replay evidence is a separate
    :class:`ReplayRecord`, so nothing a processor writes here can seed,
    replace or erase trusted history.
    """

    payment_id: str
    amount_required: Decimal
    backend: str = ""
    captured_funds: Decimal = Decimal("0")
    refunded_funds: Decimal = Decimal("0")
    remaining_authorization: Decimal = Decimal("0")
    status: str = PaymentStatus.NEW
    external_id: str | None = None
    fraud_status: str = FraudStatus.UNKNOWN
    fraud_message: str = ""
    reconciliation_required: bool = False
    provider_data: Mapping[str, Any] = field(default=EMPTY_METADATA)
    observation_conflicts: tuple["ObservationConflict", ...] = ()

    def __post_init__(self) -> None:
        validate_provider_metadata(self.provider_data, name="Payment metadata")
        object.__setattr__(
            self, "provider_data", freeze_metadata(self.provider_data)
        )


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    """Trusted evidence that one provider observation was applied.

    Replay evidence is core-owned and lives outside the unrestricted
    ``provider_data`` mapping, so processor metadata can neither seed nor
    erase it. Identity is scoped to the provider/payment context it
    arrived in, and reuse is detected by normalized semantic content
    rather than raw-payload byte equality.
    """

    payment_id: str
    backend: str
    event_identity: str
    content_digest: str

    @property
    def scoped_identity(self) -> tuple[str, str, str]:
        """The context a provider event identity is unique within.

        Two observations are the *same* event only within one payment at
        one provider. An identity a different backend issued is a
        different event, however it is spelled.
        """
        return (self.payment_id, self.backend, self.event_identity)

    @classmethod
    def for_observation(
        cls, facts: "PaymentFacts", update: PaymentUpdate
    ) -> "ReplayRecord":
        """Build the record a given observation would commit.

        The scope comes from the payment's *stored* facts, never from the
        observation: a payload cannot nominate the context its identity
        is compared in.
        """
        identity = validate_event_identity(update.provider_event_id)
        if identity is None:
            raise InvalidTransitionError(
                "A replay record requires a provider event identity."
            )
        return cls(
            payment_id=facts.payment_id,
            backend=facts.backend,
            event_identity=identity,
            content_digest=observation_digest(update),
        )


def observation_content(update: PaymentUpdate) -> str:
    """Serialize allowlisted semantic evidence, excluding provider metadata.

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
    if isinstance(update, PaymentObservation):
        outcome = update.outcome
        parts += (
            update.operation_id or "",
            str(outcome.state) if outcome else "",
            _canonical_amount(outcome.settled_amount) if outcome else "",
            (outcome.correlation or "") if outcome else "",
            str(outcome.reconciliation_required) if outcome else "",
            (outcome.external_id or "") if outcome else "",
        )
    return json.dumps(parts, ensure_ascii=True, separators=(",", ":"))


def observation_digest(update: PaymentUpdate) -> str:
    """Hash normalized semantic content with unambiguous field boundaries."""
    return sha256(observation_content(update).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservationConflict:
    """Compact disputed evidence, separate from financial facts and metadata.

    ``semantic_content`` is JSON containing only core-defined observation
    fields, not raw provider payloads. Preserve it for investigation; a digest
    alone cannot recover the financial claim that was refused.
    """

    event_identity: str | None
    semantic_content: str
    reason: str


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
    parameters: Mapping[str, Any] = field(default=EMPTY_METADATA)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_type", OperationType(self.operation_type)
        )
        _validate_operation_id(self.operation_id)
        if self.amount is not None and (
            not isinstance(self.amount, Decimal) or not self.amount.is_finite()
        ):
            raise InvalidTransitionError(
                "Operation amount must be a finite Decimal."
            )
        object.__setattr__(
            self, "parameters", freeze_parameters(self.parameters)
        )

    @property
    def parameters_digest(self) -> str:
        """Digest binding the identity to its request semantics.

        Each value is rendered with its type, so a parameter that changed
        from ``Decimal("100")`` to ``"100"`` -- or from ``True`` to
        ``"True"`` -- reads as a changed intent rather than a retry.
        """
        return _request_digest(
            [str(self.operation_type), self.amount, self.parameters]
        )


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """The durable reservation and current state of one operation intent.

    Starting totals and recursively frozen ``parameters`` describe the
    reserved request, not the payment at response time. ``idempotency_key``
    derives from the backend/payment/operation identity, independent of
    attempt count. ``reservation_sequence`` orders intents within the payment
    independently of clocks, proving which later intents cannot already be
    included in a reservation's starting totals. Preserve it in storage.
    Submission time, scope and retry deadline are frozen by
    the first claim. ``settled_amount`` retains confirmed operation-specific
    money for detecting contradictory terminal evidence.
    ``conflicting_outcomes`` retains normalized disputed evidence
    independently of established facts;
    persist it atomically with the record and reconciliation flags.
    """

    payment_id: str
    operation_id: str
    operation_type: OperationType
    state: OperationState
    resolved_amount: Decimal | None
    parameters_digest: str
    starting_captured: Decimal
    starting_refunded: Decimal
    parameters: Mapping[str, Any] = field(default=EMPTY_METADATA)
    starting_authorization: Decimal = Decimal("0")
    backend: str = ""
    reservation_sequence: int = 0
    idempotency_key: str = field(init=False)
    submitted_at: datetime | None = None
    submission_attempts: int = 0
    retry_until: datetime | None = None
    idempotency_scope: str | None = None
    settled_amount: Decimal | None = None
    correlation: str | None = None
    reconciliation_required: bool = False
    conflicting_outcomes: tuple["OperationOutcome", ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "operation_type", OperationType(self.operation_type)
        )
        object.__setattr__(self, "state", OperationState(self.state))
        _validate_operation_id(self.operation_id)
        object.__setattr__(
            self, "parameters", freeze_parameters(self.parameters)
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _request_digest([self.backend, self.payment_id, self.operation_id]),
        )

    @property
    def is_active(self) -> bool:
        """Whether this operation still holds the payment."""
        return self.state in ACTIVE_OPERATION_STATES


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    """Normalized evidence about what happened to an operation.

    ``correlation`` is the safe provider handle for the operation, kept
    per operation rather than overwriting a single payment-wide id.
    ``external_id`` is the optional payment handle established by prepare.
    ``SUCCEEDED`` confirms the operation-specific effect, not acceptance;
    omitted ``settled_amount`` then confirms the full reserved amount.
    """

    state: OperationState
    settled_amount: Decimal | None = None
    correlation: str | None = None
    reconciliation_required: bool = False
    external_id: str | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "state", OperationState(self.state))
        except (ValueError, TypeError) as exc:
            raise InvalidTransitionError(
                "Invalid operation outcome state."
            ) from exc


@dataclass(slots=True)
class PaymentObservation(PaymentUpdate):
    """Authenticated cross-channel evidence, optionally tied to an intent.

    A processor supplies ``operation_id`` only from a verified provider echo
    of the merchant identity. Alternatively ``outcome.correlation`` must
    uniquely match a retained provider handle. Equal amounts and the active
    operation are never correlation. Aggregate amounts remain cumulative;
    ``outcome.settled_amount`` describes only the identified operation.
    """

    operation_id: str | None = None
    outcome: OperationOutcome | None = None


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
    operations: tuple[OperationRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ReservationPlan:
    """What an adapter must commit to reserve an operation intent.

    ``created`` is false when the same operation identity and parameters
    were already reserved: the caller resumes that reservation instead of
    starting a second one. Commit ``facts`` with the operation when present:
    a reserved refund immediately projects refund-in-progress without
    changing financial totals.
    """

    operation: OperationRecord
    created: bool
    facts: PaymentFacts | None = None


@dataclass(frozen=True, slots=True)
class SubmissionPlan:
    """A committed operation and whether this caller won submission rights."""

    operation: OperationRecord
    granted: bool


@dataclass(frozen=True, slots=True)
class OutcomePlan:
    """What an adapter must commit when an operation resolves.

    The operation record and the financial facts it settles commit
    together, so terminal evidence and its money never diverge. Every
    ``related_operations`` entry also commits atomically: a successful
    cancellation resolves its still-unresolved target refund.
    """

    operation: OperationRecord
    facts: PaymentFacts
    related_operations: tuple[OperationRecord, ...] = ()
