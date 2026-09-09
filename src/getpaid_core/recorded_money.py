"""Upcoming-stack pure plans for attributed money recorded outside providers.

These values are not storage. Integrations must commit the new entry and facts
atomically against locked/reloaded complete history. See docs/recorded-money.md.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Final
from typing import cast

from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.rules import _FactsPayment
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.fsm import project_payment_status


if TYPE_CHECKING:
    from getpaid_core.protocols import Payment


RECORDED_MONEY_BACKEND: Final = "getpaid.recorded_money"


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
) -> RecordedMoneyPlan:
    """Plan a recorded receipt using the shared financial status projection."""
    if facts.backend != RECORDED_MONEY_BACKEND:
        raise InvalidTransitionError("A recorded-money root is required.")
    if command.kind is not RecordedMoneyKind.RECEIPT:
        raise InvalidTransitionError("Unsupported recorded-money command.")
    entry = RecordedMoneyEntry(
        payment_id=facts.payment_id,
        command=command,
        recorded_at=now,
        signed_amount=command.amount,
        effective_at=command.occurred_at,
        evidence_reference=command.evidence_reference,
    )
    projection = project_recorded_money((*history, entry))
    updated = replace(facts, captured_funds=projection.captured_funds)
    status = project_payment_status(cast("Payment", _FactsPayment(updated)))
    return RecordedMoneyPlan(
        replace(updated, status=status), entry, projection, applied=True
    )
