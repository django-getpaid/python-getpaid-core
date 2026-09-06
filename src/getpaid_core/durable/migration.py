"""Reading released 3.x payment records into the durable contract.

Core owns the *mapping*, not the migration: this module says what one
stored legacy payment becomes under the durable contract and what about
it cannot be trusted. Running the migration -- reading rows, writing
them back, ordering it against a writer cutover -- belongs to the
framework wrapper, along with its schema changes (ADR 0001, section 6).

Two things are deliberately never produced here:

* **Replay evidence.** The released contract kept its applied-event list
  inside ``provider_data``, where processor metadata could overwrite it.
  History that provider payloads had write access to cannot be certified
  after the fact, so it migrates as ordinary readable metadata and never
  as a :class:`~getpaid_core.durable.records.ReplayRecord`.
* **Operation records.** The released contract recorded no operation
  identity, and inventing one would give a historical retry a fresh
  reservation it never had.

A record whose financial state is ambiguous, or which was left in the
middle of a money-moving operation, migrates *readable but
mutation-blocked*: its facts carry ``reconciliation_required``, so
callbacks and reconciliation continue while new commands are refused
until the application settles what happened.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from getpaid_core._amounts import validate_payment_amounts
from getpaid_core.durable.records import LEGACY_REPLAY_METADATA_KEYS
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.records import validate_provider_metadata
from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError


if TYPE_CHECKING:
    from getpaid_core.protocols import Payment


_EMPTY: Mapping[str, Any] = MappingProxyType({})

_AMOUNT_FIELDS = (
    "amount_required",
    "amount_paid",
    "amount_locked",
    "amount_refunded",
)

#: Released statuses that record a money-moving operation still in
#: flight. The released contract carried no operation record, so nothing
#: says whether the provider went on to execute it.
LEGACY_PENDING_STATUSES: frozenset[PaymentStatus] = frozenset(
    {PaymentStatus.IN_CHARGE, PaymentStatus.REFUND_STARTED}
)


class MigrationFinding(StrEnum):
    """What migrating one legacy payment established about it.

    ``AMBIGUOUS_FINANCIAL_RECORD`` -- the stored balances break the
    financial invariants, or the status is not one core defines. The
    amounts are preserved as found; what they mean is not established.

    ``PENDING_OPERATION`` -- the record was left mid-operation, and the
    released contract kept no operation identity to resolve it against.

    ``UNPROMOTED_EVENT_HISTORY`` -- the record carried a legacy
    applied-event list. It stays readable as metadata and is not promoted
    to trusted evidence, so a redelivery of one of those events applies
    again. Cumulative observations make that harmless to the totals; it
    is reported because exactly-once delivery is no longer claimed for
    the payment's past.
    """

    AMBIGUOUS_FINANCIAL_RECORD = "ambiguous_financial_record"
    PENDING_OPERATION = "pending_operation"
    UNPROMOTED_EVENT_HISTORY = "unpromoted_event_history"


#: Findings that leave the migrated payment mutation-blocked. An
#: unpromoted event history is not one of them: every legacy payment that
#: ever saw a callback carries one, and blocking them all would migrate
#: the whole estate into reconciliation.
BLOCKING_MIGRATION_FINDINGS: frozenset[MigrationFinding] = frozenset(
    {
        MigrationFinding.AMBIGUOUS_FINANCIAL_RECORD,
        MigrationFinding.PENDING_OPERATION,
    }
)


@dataclass(frozen=True, slots=True)
class LegacyPaymentState:
    """One payment as the released 3.x contract stored it.

    This is the framework-neutral serialized shape core reads: an adapter
    builds it from its own rows, or from a model instance through
    :meth:`from_payment`. Core never reaches into the adapter's storage
    itself.
    """

    payment_id: str
    amount_required: Decimal
    backend: str = ""
    amount_paid: Decimal = Decimal("0")
    amount_locked: Decimal = Decimal("0")
    amount_refunded: Decimal = Decimal("0")
    status: str = PaymentStatus.NEW
    external_id: str | None = None
    fraud_status: str = FraudStatus.UNKNOWN
    fraud_message: str = ""
    provider_data: Mapping[str, Any] = field(default=_EMPTY)

    def __post_init__(self) -> None:
        validate_provider_metadata(
            self.provider_data, name="Legacy payment metadata"
        )
        object.__setattr__(
            self,
            "provider_data",
            _EMPTY
            if not self.provider_data
            else MappingProxyType(dict(self.provider_data)),
        )

    @classmethod
    def from_payment(cls, payment: "Payment") -> "LegacyPaymentState":
        """Read a legacy payment object without writing to it."""
        return cls(
            payment_id=payment.id,
            amount_required=payment.amount_required,
            backend=payment.backend,
            amount_paid=payment.amount_paid,
            amount_locked=payment.amount_locked,
            amount_refunded=payment.amount_refunded,
            status=payment.status,
            external_id=payment.external_id,
            fraud_status=payment.fraud_status,
            fraud_message=payment.fraud_message,
            provider_data=payment.provider_data,
        )


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """What an adapter must write for one migrated legacy payment.

    The plan is the whole of it: there is no replay log and no operation
    list to write alongside, because neither can be reconstructed from a
    released record honestly.
    """

    facts: PaymentFacts
    findings: tuple[MigrationFinding, ...]

    @property
    def mutation_blocked(self) -> bool:
        """Whether the migrated payment refuses new commands.

        This reads the committed fact rather than restating it: the
        reconciliation requirement on the facts is what
        :func:`~getpaid_core.durable.rules.plan_reservation` enforces.
        """
        return self.facts.reconciliation_required


def _require_representable_amounts(legacy: LegacyPaymentState) -> None:
    """Refuse a record whose balances are not money at all.

    A balance that is not a finite ``Decimal`` cannot be stored as a
    financial fact or compared against one, so there is nothing to
    migrate and nothing to reconcile against. This is distinct from an
    *ambiguous* record, whose amounts are real money that does not add
    up: that one migrates, blocked.
    """
    for name in _AMOUNT_FIELDS:
        amount = getattr(legacy, name)
        if not isinstance(amount, Decimal) or not amount.is_finite():
            raise InvalidTransitionError(
                f"Legacy payment {legacy.payment_id!r} cannot be migrated: "
                f"{name} is not a finite Decimal."
            )


def _financial_record_is_ambiguous(legacy: LegacyPaymentState) -> bool:
    """Whether the stored balances and status establish one meaning."""
    try:
        PaymentStatus(legacy.status)
        validate_payment_amounts(cast("Payment", legacy))
    except (InvalidTransitionError, ValueError):
        return True
    return False


def _carries_legacy_event_history(legacy: LegacyPaymentState) -> bool:
    return any(
        legacy.provider_data.get(key) for key in LEGACY_REPLAY_METADATA_KEYS
    )


def plan_migration(legacy: LegacyPaymentState) -> MigrationPlan:
    """Map one released payment record onto durable facts.

    Amounts and metadata are preserved exactly as stored -- including any
    legacy applied-event list, which stays readable and untrusted. No
    replay record and no operation record is produced.

    Raises ``InvalidTransitionError`` when the record's balances are not
    finite ``Decimal`` money, which is a source-data repair rather than
    something reconciliation can settle.
    """
    _require_representable_amounts(legacy)

    findings: list[MigrationFinding] = []
    if _financial_record_is_ambiguous(legacy):
        findings.append(MigrationFinding.AMBIGUOUS_FINANCIAL_RECORD)
    if legacy.status in LEGACY_PENDING_STATUSES:
        findings.append(MigrationFinding.PENDING_OPERATION)
    if _carries_legacy_event_history(legacy):
        findings.append(MigrationFinding.UNPROMOTED_EVENT_HISTORY)

    return MigrationPlan(
        facts=PaymentFacts(
            payment_id=legacy.payment_id,
            amount_required=legacy.amount_required,
            backend=legacy.backend,
            captured_funds=legacy.amount_paid,
            refunded_funds=legacy.amount_refunded,
            remaining_authorization=legacy.amount_locked,
            status=legacy.status,
            external_id=legacy.external_id,
            fraud_status=legacy.fraud_status,
            fraud_message=legacy.fraud_message,
            reconciliation_required=any(
                finding in BLOCKING_MIGRATION_FINDINGS for finding in findings
            ),
            provider_data=legacy.provider_data,
        ),
        findings=tuple(findings),
    )
