"""Core validation and transition rules for durable money operations.

Every function here is pure: it takes the payment's *current* durable
facts plus the evidence to apply, and returns the records an adapter must
commit atomically. Core owns these rules so no framework wrapper has to
reimplement them; the adapter owns only the transaction, lock or
compare-and-set that makes the commit atomic (ADR 0001, section 1).
"""

from collections.abc import Iterable
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from getpaid_core._amounts import validate_amount
from getpaid_core.durable.records import TERMINAL_OPERATION_STATES
from getpaid_core.durable.records import ObservationPlan
from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationRecord
from getpaid_core.durable.records import OperationState
from getpaid_core.durable.records import OperationType
from getpaid_core.durable.records import OutcomePlan
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.records import ReplayRecord
from getpaid_core.durable.records import ReservationPlan
from getpaid_core.enums import PaymentEvent
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.fsm import apply_payment_update
from getpaid_core.types import PaymentUpdate


if TYPE_CHECKING:
    from getpaid_core.protocols import Payment


class _FactsPayment:
    """A ``Payment``-protocol view over :class:`PaymentFacts`.

    The state engine is written against mutable payment objects. Rather
    than restate its transition rules for facts, plans run the engine over
    this throwaway view of the *current durable* facts and read the result
    back. Nothing the caller owns is touched.

    It carries the fields the state engine reads, which is a subset of the
    ``Payment`` protocol: the order, currency, backend and description of
    a payment are not financial facts and no transition consults them.
    """

    def __init__(self, facts: PaymentFacts) -> None:
        self.id = facts.payment_id
        self.amount_required = facts.amount_required
        self.amount_paid = facts.captured_funds
        self.amount_refunded = facts.refunded_funds
        self.amount_locked = facts.remaining_authorization
        self.status = facts.status
        self.external_id = facts.external_id
        self.fraud_status = facts.fraud_status
        self.fraud_message = facts.fraud_message
        self.provider_data: dict[str, Any] = dict(facts.provider_data)
        self._payment_id = facts.payment_id
        self._reconciliation_required = facts.reconciliation_required

    def to_facts(self) -> PaymentFacts:
        return PaymentFacts(
            payment_id=self._payment_id,
            amount_required=self.amount_required,
            captured_funds=self.amount_paid,
            refunded_funds=self.amount_refunded,
            remaining_authorization=self.amount_locked,
            status=self.status,
            external_id=self.external_id,
            fraud_status=self.fraud_status,
            fraud_message=self.fraud_message,
            reconciliation_required=self._reconciliation_required,
            provider_data=self.provider_data,
        )


def _apply_to_facts(facts: PaymentFacts, update: PaymentUpdate) -> PaymentFacts:
    """Run the state engine against current facts and return the result.

    Replay bookkeeping is stripped from the update: it is core-owned
    evidence committed as a :class:`ReplayRecord`, never as an entry in
    the provider metadata mapping.
    """
    view = _FactsPayment(facts)
    apply_payment_update(
        cast("Payment", view), replace(update, provider_event_id=None)
    )
    return view.to_facts()


def plan_observation(
    facts: PaymentFacts,
    replay_log: Iterable[ReplayRecord],
    update: PaymentUpdate | None,
) -> ObservationPlan:
    """Plan the atomic application of one provider observation.

    ``facts`` must be the payment's current durable state, read inside
    the same atomic boundary that will commit the plan. A stale or equal
    cumulative observation is applied without regressing committed funds;
    an already-seen event identity applies nothing; the same identity
    carrying different semantic content is refused and flagged for
    reconciliation rather than silently suppressing a financial change.

    Raises ``InvalidTransitionError`` for evidence that cannot be applied
    to the current state: an impossible transition stays an error rather
    than a blanket ignored exception.
    """
    if update is None:
        return ObservationPlan(facts=facts, replay_record=None, applied=False)

    record: ReplayRecord | None = None
    if update.provider_event_id:
        record = ReplayRecord.for_observation(facts.payment_id, update)
        for known in replay_log:
            if known.event_identity != record.event_identity:
                continue
            if known.content_digest == record.content_digest:
                return ObservationPlan(
                    facts=facts, replay_record=None, applied=False
                )
            return ObservationPlan(
                facts=replace(facts, reconciliation_required=True),
                replay_record=None,
                applied=False,
            )

    return ObservationPlan(
        facts=_apply_to_facts(facts, update),
        replay_record=record,
        applied=True,
    )


def _capturable(facts: PaymentFacts) -> Decimal:
    return min(
        facts.remaining_authorization,
        facts.amount_required - facts.captured_funds,
    )


def _refundable(facts: PaymentFacts) -> Decimal:
    return facts.captured_funds - facts.refunded_funds


def _resolve_amount(
    facts: PaymentFacts, intent: OperationIntent
) -> Decimal | None:
    """Resolve the intent's concrete amount against current facts.

    The result is frozen onto the reservation: a same-ID retry reuses it
    rather than reselecting a default against a later balance.
    """
    operation_type = intent.operation_type

    if operation_type in {OperationType.PREPARE, OperationType.CANCEL_REFUND}:
        return None

    if operation_type is OperationType.RELEASE_LOCK:
        available = facts.remaining_authorization
        validate_amount(
            available, "Remaining authorization", allow_zero=False
        )
        if intent.amount is not None and intent.amount != available:
            raise InvalidTransitionError(
                "Authorization release covers the whole remaining "
                f"authorization {available}, not {intent.amount}."
            )
        return available

    if operation_type is OperationType.CHARGE:
        available, name = _capturable(facts), "Charge amount"
    else:
        available, name = _refundable(facts), "Refund amount"

    amount = available if intent.amount is None else intent.amount
    validate_amount(amount, name, allow_zero=False, maximum=available)
    return amount


def _blocking_operation(
    operations: Iterable[OperationRecord], intent: OperationIntent
) -> OperationRecord | None:
    """Find the outstanding operation that forbids this new intent.

    Only one mutation may be active per payment, so that two reservations
    never overlap on the same funds. The single exception is a refund
    cancellation, which is allowed to target the pending refund it names.
    """
    target = intent.parameters.get("target_operation_id")
    for record in operations:
        if not record.is_active:
            continue
        if record.operation_id == intent.operation_id:
            continue
        if (
            intent.operation_type is OperationType.CANCEL_REFUND
            and record.operation_id == target
        ):
            continue
        return record
    return None


def _cancellation_target(
    operations: Iterable[OperationRecord], intent: OperationIntent
) -> None:
    """Refuse a cancellation that does not name an outstanding refund."""
    target = intent.parameters.get("target_operation_id")
    matched = any(
        record.operation_id == target
        and record.operation_type is OperationType.START_REFUND
        and record.is_active
        for record in operations
    )
    if not matched:
        raise OperationConflictError(
            "A refund cancellation must name the pending refund it cancels "
            "through parameters['target_operation_id']; "
            f"{target!r} is not an outstanding refund.",
            context={"operation_id": intent.operation_id, "target": target},
        )


def plan_reservation(
    facts: PaymentFacts,
    operations: Iterable[OperationRecord],
    intent: OperationIntent,
) -> ReservationPlan:
    """Plan the durable reservation of one operation intent.

    ``facts`` and ``operations`` must be the payment's current committed
    state. A repeat of the same operation ID with the same parameters
    resumes the existing reservation; the same ID with different
    parameters, or an unrelated command while another mutation is
    outstanding, raises ``OperationConflictError``.

    Reserving is not provider acceptance and not settlement: it records
    what was resolved and who holds the payment.
    """
    operations = tuple(operations)
    digest = intent.parameters_digest

    for record in operations:
        if record.operation_id != intent.operation_id:
            continue
        if record.parameters_digest != digest:
            raise OperationConflictError(
                f"Operation {intent.operation_id!r} was reserved with "
                "different parameters; a changed intent needs its own "
                "operation ID.",
                context={"operation_id": intent.operation_id},
            )
        return ReservationPlan(operation=record, created=False)

    if intent.operation_type is OperationType.CANCEL_REFUND:
        _cancellation_target(operations, intent)

    blocker = _blocking_operation(operations, intent)
    if blocker is not None:
        raise OperationConflictError(
            f"Operation {blocker.operation_id!r} is still "
            f"{blocker.state.value!r} on payment "
            f"{facts.payment_id!r}; resolve it before reserving "
            f"{intent.operation_id!r}.",
            context={
                "payment_id": facts.payment_id,
                "blocking_operation_id": blocker.operation_id,
            },
        )

    return ReservationPlan(
        operation=OperationRecord(
            payment_id=facts.payment_id,
            operation_id=intent.operation_id,
            operation_type=intent.operation_type,
            state=OperationState.RESERVED,
            resolved_amount=_resolve_amount(facts, intent),
            parameters_digest=digest,
            starting_captured=facts.captured_funds,
            starting_refunded=facts.refunded_funds,
        ),
        created=True,
    )


def _settlement_update(
    operation: OperationRecord, outcome: OperationOutcome
) -> PaymentUpdate:
    """Build the cumulative settlement a succeeded operation establishes.

    Totals are derived from the reservation's *starting* totals plus the
    settled amount, never from whatever the payment holds now: a callback
    that already reported the same money must not be counted twice.
    """
    operation_type = operation.operation_type
    settled = (
        operation.resolved_amount
        if outcome.settled_amount is None
        else outcome.settled_amount
    )

    if operation_type is OperationType.PREPARE:
        return PaymentUpdate(payment_event=PaymentEvent.PREPARED)
    if operation_type is OperationType.RELEASE_LOCK:
        return PaymentUpdate(payment_event=PaymentEvent.LOCK_RELEASED)
    if operation_type is OperationType.CANCEL_REFUND:
        return PaymentUpdate(payment_event=PaymentEvent.REFUND_CANCELLED)

    if settled is None:
        raise InvalidTransitionError(
            f"A succeeded {operation_type.value} needs a settled amount."
        )
    validate_amount(settled, f"{operation_type.value} settled amount")

    if operation_type is OperationType.CHARGE:
        return PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=operation.starting_captured + settled,
        )
    return PaymentUpdate(
        payment_event=PaymentEvent.REFUND_CONFIRMED,
        refunded_amount=operation.starting_refunded + settled,
    )


def plan_outcome(
    facts: PaymentFacts,
    operation: OperationRecord,
    outcome: OperationOutcome,
) -> OutcomePlan:
    """Plan the atomic recording of an operation outcome.

    The operation record and the financial facts it settles are returned
    together so the adapter commits them in one boundary. A nonterminal
    outcome -- including ``UNKNOWN`` -- moves no money and leaves the
    operation discoverable as unresolved work.
    """
    current = operation.state
    if current in TERMINAL_OPERATION_STATES:
        if outcome.state is not current:
            raise InvalidTransitionError(
                f"Operation {operation.operation_id!r} is already "
                f"{current.value!r} and cannot become "
                f"{outcome.state.value!r}."
            )
        return OutcomePlan(operation=operation, facts=facts)

    settled_facts = facts
    if outcome.state is OperationState.SUCCEEDED:
        update = _settlement_update(operation, outcome)
        settled_facts = _apply_to_facts(facts, update)

    return OutcomePlan(
        operation=replace(
            operation,
            state=outcome.state,
            correlation=outcome.correlation or operation.correlation,
            reconciliation_required=(
                operation.reconciliation_required
                or outcome.reconciliation_required
            ),
        ),
        facts=settled_facts,
    )
