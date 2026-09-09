"""Upcoming-stack pure plans for attributed money recorded outside providers.

These values are not storage. Integrations must commit the new entry and facts
atomically against locked/reloaded complete history. See docs/recorded-money.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Final
from typing import cast

from getpaid_core.durable.rules import _FactsPayment
from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.fsm import project_payment_status


if TYPE_CHECKING:
    from collections.abc import Iterable

    from getpaid_core.durable.records import OperationRecord
    from getpaid_core.durable.records import PaymentFacts
    from getpaid_core.durable.records import ReplayRecord
    from getpaid_core.protocols import Payment


RECORDED_MONEY_BACKEND: Final = "getpaid.recorded_money"


def _text(value: str, name: str, *, maximum: int = 256) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise InvalidTransitionError(f"Invalid {name} shape.")


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise InvalidTransitionError("An aware datetime is required.")
    return value.astimezone(UTC)


def _amount(value: Decimal, *, positive: bool = False) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidTransitionError("Money must be a finite Decimal.")
    if value < 0 or (positive and value == 0):
        raise InvalidTransitionError("Money is outside the permitted bounds.")


class RecordedMoneyKind(StrEnum):
    """An assertion of received money, not a provider operation."""

    RECEIPT = "receipt"


@dataclass(frozen=True, slots=True, repr=False)
class RecordedMoneyCommand:
    """An attributed command identity, scoped to one payment root."""

    command_id: str
    kind: RecordedMoneyKind
    actor: str
    amount: Decimal
    occurred_at: datetime
    note: str | None = None
    evidence_reference: str | None = None

    def __post_init__(self) -> None:
        _text(self.command_id, "command identity")
        _text(self.actor, "actor")
        if not isinstance(self.kind, RecordedMoneyKind):
            raise InvalidTransitionError(
                "A typed recorded-money kind is required."
            )
        if self.note is not None:
            _text(self.note, "note", maximum=2000)
        if self.evidence_reference is not None:
            _text(self.evidence_reference, "evidence reference")
        _amount(self.amount, positive=True)
        object.__setattr__(self, "occurred_at", _aware(self.occurred_at))


@dataclass(frozen=True, slots=True, repr=False)
class RecordedMoneyEntry:
    """Immutable retained evidence; never update or delete an older entry."""

    payment_id: str
    command: RecordedMoneyCommand
    recorded_at: datetime
    signed_amount: Decimal
    effective_at: datetime
    evidence_reference: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class RecordedMoneyProjection:
    """Complete history and the effective movements it currently asserts."""

    records: tuple[RecordedMoneyEntry, ...]
    effective_receipts: tuple[RecordedMoneyEntry, ...]
    effective_repayments: tuple[RecordedMoneyEntry, ...]
    captured_funds: Decimal
    refunded_funds: Decimal


@dataclass(frozen=True, slots=True, repr=False)
class RecordedMoneyPlan:
    """Commit entry and facts together; on replay entry is already retained."""

    facts: PaymentFacts
    entry: RecordedMoneyEntry
    projection: RecordedMoneyProjection
    applied: bool


def project_recorded_money(
    history: Iterable[RecordedMoneyEntry],
) -> RecordedMoneyProjection:
    """Project receipt evidence without changing the original records."""
    records = tuple(history)
    seen: set[str] = set()
    for entry in records:
        if not isinstance(entry, RecordedMoneyEntry):
            raise InvalidTransitionError("Invalid retained record type.")
        _text(entry.payment_id, "payment identity")
        command = entry.command
        if not isinstance(command, RecordedMoneyCommand):
            raise InvalidTransitionError("Invalid retained command type.")
        if command.command_id in seen:
            raise InvalidTransitionError("Duplicate retained command identity.")
        seen.add(command.command_id)
        if entry.payment_id != records[0].payment_id:
            raise InvalidTransitionError("History belongs to another payment.")
        if (
            _aware(entry.effective_at) != command.occurred_at
            or entry.effective_at > _aware(entry.recorded_at)
            or entry.evidence_reference != command.evidence_reference
            or not isinstance(entry.signed_amount, Decimal)
            or not entry.signed_amount.is_finite()
            or entry.signed_amount != command.amount
        ):
            raise InvalidTransitionError(
                "Retained evidence disagrees with command."
            )
    return RecordedMoneyProjection(
        records=records,
        effective_receipts=records,
        effective_repayments=(),
        captured_funds=sum((r.signed_amount for r in records), Decimal("0")),
        refunded_funds=Decimal("0"),
    )


def plan_recorded_money(
    facts: PaymentFacts,
    history: Iterable[RecordedMoneyEntry],
    command: RecordedMoneyCommand,
    *,
    now: datetime,
    operations: Iterable[OperationRecord] = (),
    replay_records: Iterable[ReplayRecord] = (),
) -> RecordedMoneyPlan:
    """Plan against current facts and complete retained histories; no I/O."""
    now = _aware(now)
    _text(facts.payment_id, "payment identity")
    for value in (
        facts.captured_funds,
        facts.refunded_funds,
        facts.remaining_authorization,
    ):
        _amount(value)
    _amount(facts.amount_required, positive=True)
    if (
        facts.backend != RECORDED_MONEY_BACKEND
        or tuple(operations)
        or tuple(replay_records)
        or facts.external_id is not None
        or facts.provider_data
        or facts.remaining_authorization != 0
        or facts.reconciliation_required
        or facts.observation_conflicts
        or facts.fraud_status != FraudStatus.UNKNOWN
        or facts.fraud_message
    ):
        raise InvalidTransitionError(
            "An uncontaminated recorded-money root is required."
        )
    current = project_recorded_money(history)
    if (
        current.captured_funds != facts.captured_funds
        or current.refunded_funds != facts.refunded_funds
        or facts.captured_funds > facts.amount_required
        or any(
            entry.payment_id != facts.payment_id or entry.recorded_at > now
            for entry in current.records
        )
        or facts.status
        not in {
            PaymentStatus.NEW,
            PaymentStatus.PREPARED,
            PaymentStatus.PARTIAL,
            PaymentStatus.PAID,
            PaymentStatus.PARTIALLY_REFUNDED,
            PaymentStatus.REFUNDED,
        }
        or project_payment_status(cast("Payment", _FactsPayment(facts)))
        != facts.status
    ):
        raise InvalidTransitionError(
            "Stored summary and history disagree; reconcile first."
        )
    if not isinstance(command, RecordedMoneyCommand):
        raise InvalidTransitionError("A recorded-money command is required.")
    if command.occurred_at > now:
        raise InvalidTransitionError("Business time cannot be in the future.")
    for retained in current.records:
        if retained.command.command_id == command.command_id:
            if retained.command != command:
                raise OperationConflictError(
                    "Recorded command identity conflicts."
                )
            return RecordedMoneyPlan(facts, retained, current, applied=False)
    entry = RecordedMoneyEntry(
        payment_id=facts.payment_id,
        command=command,
        recorded_at=now,
        signed_amount=command.amount,
        effective_at=command.occurred_at,
        evidence_reference=command.evidence_reference,
    )
    projection = project_recorded_money((*current.records, entry))
    if projection.captured_funds > facts.amount_required:
        raise InvalidTransitionError(
            "Recorded receipts exceed the required amount."
        )
    updated = replace(facts, captured_funds=projection.captured_funds)
    status = project_payment_status(cast("Payment", _FactsPayment(updated)))
    return RecordedMoneyPlan(
        replace(updated, status=status), entry, projection, applied=True
    )
