"""Upcoming-stack pure plans for attributed money recorded outside providers.

These values are not storage. Integrations must commit the new entry and facts
atomically against locked/reloaded complete history. See docs/recorded-money.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from decimal import MAX_EMAX
from decimal import MIN_EMIN
from decimal import Decimal
from decimal import localcontext
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
    """Recorded movements and corrections, never provider operations."""

    RECEIPT = "receipt"
    REPAYMENT = "repayment"
    CORRECTION = "correction"


class RecordingCorrectionReason(StrEnum):
    """Why a previously asserted movement must no longer be effective."""

    MISTAKEN_ENTRY = "mistaken_entry"
    INCORRECT_AMOUNT = "incorrect_amount"
    INCORRECT_TIMESTAMP = "incorrect_timestamp"
    INCORRECT_REFERENCE = "incorrect_reference"
    DUPLICATE_ENTRY = "duplicate_entry"


@dataclass(frozen=True, slots=True, repr=False)
class RecordedMoneyCommand:
    """An attributed command identity, scoped to one payment root."""

    command_id: str
    kind: RecordedMoneyKind
    actor: str
    amount: Decimal | None = None
    occurred_at: datetime | None = None
    note: str | None = None
    evidence_reference: str | None = None
    target_command_id: str | None = None
    correction_reason: RecordingCorrectionReason | None = None

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
        if (
            self.kind is RecordedMoneyKind.REPAYMENT
            and self.evidence_reference is None
        ):
            raise InvalidTransitionError(
                "Repayment requires external evidence."
            )
        if self.kind is RecordedMoneyKind.CORRECTION:
            if not isinstance(self.target_command_id, str):
                raise InvalidTransitionError(
                    "Correction requires a target identity."
                )
            _text(self.target_command_id, "correction target")
            if not isinstance(
                self.correction_reason, RecordingCorrectionReason
            ):
                raise InvalidTransitionError(
                    "Correction requires a typed reason."
                )
            if any(
                value is not None
                for value in (
                    self.amount,
                    self.occurred_at,
                    self.evidence_reference,
                )
            ):
                raise InvalidTransitionError(
                    "Correction derives money, time and evidence."
                )
        else:
            if (
                self.target_command_id is not None
                or self.correction_reason is not None
            ):
                raise InvalidTransitionError(
                    "Only corrections can name a target or reason."
                )
            if self.amount is None or self.occurred_at is None:
                raise InvalidTransitionError(
                    "Movements require amount and business time."
                )
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


def _sum_exact(values: Iterable[Decimal]) -> Decimal:
    """Add finite amounts without ambient precision/exponent rounding."""
    amounts = tuple(values)
    if not amounts:
        return Decimal("0")
    least_exponent = min(cast("int", a.as_tuple().exponent) for a in amounts)
    greatest_digit = max(a.adjusted() for a in amounts)
    with localcontext() as context:
        context.prec = max(
            1, greatest_digit - least_exponent + len(str(len(amounts))) + 2
        )
        context.Emax = MAX_EMAX
        context.Emin = MIN_EMIN
        return sum(amounts, Decimal("0"))


def project_recorded_money(
    history: Iterable[RecordedMoneyEntry],
    *,
    amount_required: Decimal | None = None,
) -> RecordedMoneyProjection:
    """Validate ordered history and project its effective movements and totals.

    With ``amount_required``, every prefix also obeys the root's upper bound.
    No current clock or stored summary is available here; the planner checks
    those separately. Input entries are retained unchanged, including corrected
    originals and the counter-entries that withdrew their assertions.
    """
    if amount_required is not None:
        _amount(amount_required, positive=True)
    records = tuple(history)
    seen: dict[str, RecordedMoneyEntry] = {}
    corrected: set[str] = set()
    captured = refunded = Decimal("0")
    for entry in records:
        if not isinstance(entry, RecordedMoneyEntry):
            raise InvalidTransitionError("Invalid retained record type.")
        _text(entry.payment_id, "payment identity")
        command = entry.command
        if not isinstance(command, RecordedMoneyCommand):
            raise InvalidTransitionError("Invalid retained command type.")
        if command.command_id in seen:
            raise InvalidTransitionError("Duplicate retained command identity.")
        if entry.payment_id != records[0].payment_id:
            raise InvalidTransitionError("History belongs to another payment.")
        _aware(entry.effective_at)
        expected = _make_entry(
            entry.payment_id,
            command,
            _aware(entry.recorded_at),
            seen,
            corrected,
        )
        if (
            not isinstance(entry.signed_amount, Decimal)
            or not entry.signed_amount.is_finite()
            or entry != expected
        ):
            raise InvalidTransitionError(
                "Retained evidence disagrees with command."
            )
        original = seen.get(command.target_command_id or "", entry)
        if original.command.kind is RecordedMoneyKind.RECEIPT:
            captured = _sum_exact((captured, entry.signed_amount))
        else:
            refunded = _sum_exact((refunded, entry.signed_amount.copy_negate()))
        if not 0 <= refunded <= captured:
            raise InvalidTransitionError(
                "Retained history contains unfunded money."
            )
        if amount_required is not None and captured > amount_required:
            raise InvalidTransitionError(
                "Recorded receipts exceed the required amount."
            )
        seen[command.command_id] = entry
        if command.target_command_id is not None:
            corrected.add(command.target_command_id)
    effective = tuple(
        r for r in records if r.command.command_id not in corrected
    )
    receipts = tuple(
        r for r in effective if r.command.kind is RecordedMoneyKind.RECEIPT
    )
    repayments = tuple(
        r for r in effective if r.command.kind is RecordedMoneyKind.REPAYMENT
    )
    return RecordedMoneyProjection(
        records, receipts, repayments, captured, refunded
    )


def _make_entry(
    payment_id: str,
    command: RecordedMoneyCommand,
    now: datetime,
    retained: dict[str, RecordedMoneyEntry],
    corrected: set[str],
) -> RecordedMoneyEntry:
    if command.kind is RecordedMoneyKind.CORRECTION:
        target = retained.get(command.target_command_id or "")
        if (
            target is None
            or target.command.kind is RecordedMoneyKind.CORRECTION
        ):
            raise InvalidTransitionError(
                "Correction must target an original movement."
            )
        if target.command.command_id in corrected:
            raise InvalidTransitionError("Movement has already been corrected.")
        if target.recorded_at > now:
            raise InvalidTransitionError(
                "Correction cannot predate its recorded target."
            )
        return RecordedMoneyEntry(
            payment_id,
            command,
            now,
            target.signed_amount.copy_negate(),
            target.effective_at,
            target.evidence_reference,
        )
    # The command constructor validates the kind-specific required fields.
    assert command.amount is not None and command.occurred_at is not None
    if command.occurred_at > now:
        raise InvalidTransitionError("Business time cannot be in the future.")
    signed = command.amount
    if command.kind is RecordedMoneyKind.REPAYMENT:
        signed = signed.copy_negate()
    return RecordedMoneyEntry(
        payment_id,
        command,
        now,
        signed,
        command.occurred_at,
        command.evidence_reference,
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
    """Plan against current facts and complete retained histories; no I/O.

    The adapter must reload under its atomic boundary, supply its own aware
    clock and enforce immutable source identity. ``applied=True`` requires an
    atomic append of ``entry`` with ``facts``; replay returns the retained entry
    and unchanged current facts. Invalid inputs never produce a repair plan.
    """
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
    current = project_recorded_money(
        history, amount_required=facts.amount_required
    )
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
    if command.occurred_at is not None and command.occurred_at > now:
        raise InvalidTransitionError("Business time cannot be in the future.")
    for retained in current.records:
        if retained.command.command_id == command.command_id:
            if retained.command != command:
                raise OperationConflictError(
                    "Recorded command identity conflicts."
                )
            return RecordedMoneyPlan(facts, retained, current, applied=False)
    entry = _make_entry(
        facts.payment_id,
        command,
        now,
        {entry.command.command_id: entry for entry in current.records},
        {
            entry.command.target_command_id
            for entry in current.records
            if entry.command.target_command_id is not None
        },
    )
    projection = project_recorded_money(
        (*current.records, entry), amount_required=facts.amount_required
    )
    updated = replace(
        facts,
        captured_funds=projection.captured_funds,
        refunded_funds=projection.refunded_funds,
    )
    status = project_payment_status(cast("Payment", _FactsPayment(updated)))
    return RecordedMoneyPlan(
        replace(updated, status=status), entry, projection, applied=True
    )
