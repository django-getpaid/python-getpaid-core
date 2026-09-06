"""Core validation and transition rules for durable money operations.

Every function here is pure: it takes the payment's *current* durable
facts plus the evidence to apply, and returns the records an adapter must
commit atomically. Core owns these rules so no framework wrapper has to
reimplement them; the adapter owns only the transaction, lock or
compare-and-set that makes the commit atomic (ADR 0001, section 1).
"""

from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from getpaid_core._amounts import validate_amount
from getpaid_core._amounts import validate_payment_amounts
from getpaid_core.durable.records import CANCELLATION_CORRELATION
from getpaid_core.durable.records import CANCELLATION_TARGET
from getpaid_core.durable.records import TERMINAL_OPERATION_STATES
from getpaid_core.durable.records import ObservationConflict
from getpaid_core.durable.records import ObservationPlan
from getpaid_core.durable.records import OperationIntent
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationRecord
from getpaid_core.durable.records import OperationState
from getpaid_core.durable.records import OperationType
from getpaid_core.durable.records import OutcomePlan
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.records import PaymentObservation
from getpaid_core.durable.records import ReplayRecord
from getpaid_core.durable.records import ReservationPlan
from getpaid_core.durable.records import SubmissionPlan
from getpaid_core.durable.records import observation_content
from getpaid_core.durable.records import validate_event_identity
from getpaid_core.durable.records import validate_provider_metadata
from getpaid_core.enums import PaymentEvent
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.exceptions import ReconciliationBlockedError
from getpaid_core.fsm import apply_payment_update
from getpaid_core.fsm import capturable_amount
from getpaid_core.fsm import project_payment_status
from getpaid_core.fsm import refundable_amount
from getpaid_core.fsm import require_capture_eligible
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
    ``Payment`` protocol: the order, currency and description of a payment
    are not financial facts and no transition consults them. The context
    the engine does not read -- the payment identity, its backend and its
    reconciliation requirement -- is carried through unchanged so that
    applying an observation cannot quietly drop it.
    """

    def __init__(self, facts: PaymentFacts) -> None:
        self.id = facts.payment_id
        self.backend = facts.backend
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
        self._observation_conflicts = facts.observation_conflicts

    def to_facts(self) -> PaymentFacts:
        return PaymentFacts(
            payment_id=self._payment_id,
            amount_required=self.amount_required,
            backend=self.backend,
            captured_funds=self.amount_paid,
            refunded_funds=self.amount_refunded,
            remaining_authorization=self.amount_locked,
            status=self.status,
            external_id=self.external_id,
            fraud_status=self.fraud_status,
            fraud_message=self.fraud_message,
            reconciliation_required=self._reconciliation_required,
            provider_data=self.provider_data,
            observation_conflicts=self._observation_conflicts,
        )


def _apply_to_facts(facts: PaymentFacts, update: PaymentUpdate) -> PaymentFacts:
    """Run the state engine against current facts and return the result.

    Replay bookkeeping is stripped from the update: it is core-owned
    evidence committed as a :class:`ReplayRecord`, never as an entry in
    the provider metadata mapping.
    """
    view = _FactsPayment(facts)
    event = update.payment_event
    # Financial fields are independent cumulative claims, not payloads owned
    # exclusively by the event label. Apply capture before refund so a single
    # snapshot can establish both, then process other valid information.
    for financial_update in (
        PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=update.paid_amount,
        ),
        PaymentUpdate(
            payment_event=PaymentEvent.REFUND_CONFIRMED,
            refunded_amount=update.refunded_amount,
        ),
    ):
        if (
            financial_update.paid_amount is not None
            or financial_update.refunded_amount is not None
        ):
            apply_payment_update(cast("Payment", view), financial_update)
            if event is financial_update.payment_event:
                event = None
    apply_payment_update(
        cast("Payment", view),
        replace(
            update,
            provider_event_id=None,
            payment_event=event,
            paid_amount=None,
            refunded_amount=None,
        ),
    )
    result = view.to_facts()
    if result.captured_funds > facts.captured_funds and (
        facts.refunded_funds > 0 or facts.status == PaymentStatus.REFUND_STARTED
    ):
        result = replace(result, reconciliation_required=True)
    return result


def _retain_observation(
    facts: PaymentFacts, update: PaymentUpdate, reason: str
) -> PaymentFacts:
    evidence = ObservationConflict(
        update.provider_event_id, observation_content(update), reason
    )
    conflicts = facts.observation_conflicts
    if evidence not in conflicts:
        conflicts = (*conflicts, evidence)
    return replace(
        facts, reconciliation_required=True, observation_conflicts=conflicts
    )


def _financial_observation_is_possible(
    facts: PaymentFacts, update: PaymentUpdate
) -> bool:
    amounts = (update.paid_amount, update.refunded_amount, update.locked_amount)
    for amount in amounts:
        if amount is not None and (
            not isinstance(amount, Decimal) or not amount.is_finite()
        ):
            raise InvalidTransitionError(
                "Observation amounts must be finite Decimals."
            )
    captured = max(facts.captured_funds, update.paid_amount or Decimal("0"))
    return (
        all(amount is None or amount >= 0 for amount in amounts)
        and captured <= facts.amount_required
        and (
            update.refunded_amount is None or update.refunded_amount <= captured
        )
        and (
            update.locked_amount is None
            or (0 < update.locked_amount <= facts.amount_required - captured)
        )
    )


def plan_observation(
    facts: PaymentFacts,
    replay_log: Iterable[ReplayRecord],
    update: PaymentUpdate | None,
    *,
    operations: Iterable[OperationRecord] = (),
) -> ObservationPlan:
    """Plan the atomic application of one provider observation.

    ``facts`` must be the payment's current durable state, read inside
    the same atomic boundary that will commit the plan. A stale or equal
    cumulative observation is applied without regressing committed funds;
    an already-seen event identity applies nothing; the same identity
    carrying different semantic content is refused and flagged for
    reconciliation rather than silently suppressing a financial change.

    Raises ``InvalidTransitionError`` for evidence that cannot be applied
    to the current state, and for malformed metadata or a malformed event
    identity: an impossible transition stays an error rather than a
    blanket ignored exception, and rejecting before anything is planned
    is what leaves committed funds and committed history untouched.
    """
    if update is None:
        return ObservationPlan(facts=facts, replay_record=None, applied=False)

    validate_provider_metadata(
        update.provider_data, name="Observation metadata"
    )
    identity = validate_event_identity(update.provider_event_id)
    if not _financial_observation_is_possible(facts, update):
        return ObservationPlan(
            facts=_retain_observation(facts, update, "financial_constraints"),
            replay_record=None,
            applied=False,
        )

    record: ReplayRecord | None = None
    if identity is not None:
        record = ReplayRecord.for_observation(facts, update)
        for known in replay_log:
            if known.scoped_identity != record.scoped_identity:
                continue
            if known.content_digest == record.content_digest:
                return ObservationPlan(
                    facts=facts, replay_record=None, applied=False
                )
            return ObservationPlan(
                facts=_retain_observation(
                    facts, update, "conflicting_identity"
                ),
                replay_record=None,
                applied=False,
            )

    operations = tuple(operations)
    changed: tuple[OperationRecord, ...] = ()
    aggregate = update
    if isinstance(update, PaymentObservation) and update.delta_only:
        aggregate = replace(
            update,
            paid_amount=None,
            refunded_amount=None,
            locked_amount=None,
            payment_event=None,
        )
        if update.outcome is None:
            facts = _retain_observation(facts, update, "unresolved_delta")
    if update.payment_event in {
        PaymentEvent.LOCK_RELEASED,
        PaymentEvent.REFUND_CANCELLED,
    }:
        scoped_release = (
            isinstance(update, PaymentObservation)
            and update.cancellation_scope is OperationType.RELEASE_LOCK
            and update.payment_event is PaymentEvent.LOCK_RELEASED
        )
        correlated_outcome = (
            isinstance(update, PaymentObservation)
            and update.outcome is not None
        )
        if not scoped_release and not correlated_outcome:
            facts = _retain_observation(facts, update, "ambiguous_cancellation")
        if not scoped_release or facts.remaining_authorization == 0:
            aggregate = replace(aggregate, payment_event=None)
    result = _apply_to_facts(facts, aggregate)
    if isinstance(update, PaymentObservation) and update.outcome is not None:
        candidates = [
            operation
            for operation in operations
            if operation.payment_id == facts.payment_id
            and operation.backend == facts.backend
            and (
                operation.operation_id == update.operation_id
                if update.operation_id is not None
                else update.outcome.correlation is not None
                and operation.correlation == update.outcome.correlation
            )
        ]
        if len(candidates) != 1:
            result = _retain_observation(result, update, "uncorrelated_outcome")
        else:
            outcome_plan = plan_outcome(
                result, candidates[0], update.outcome, operations=operations
            )
            result = outcome_plan.facts
            changed = (outcome_plan.operation, *outcome_plan.related_operations)
    replacements = {operation.operation_id: operation for operation in changed}
    if any(
        replacements.get(operation.operation_id, operation).is_active
        and operation.operation_type is OperationType.START_REFUND
        for operation in operations
    ):
        result = replace(result, status=PaymentStatus.REFUND_STARTED)
    return ObservationPlan(
        facts=result, replay_record=record, applied=True, operations=changed
    )


def _resolve_amount(
    facts: PaymentFacts, intent: OperationIntent
) -> Decimal | None:
    """Resolve the intent's concrete amount against current facts.

    The result is frozen onto the reservation: a same-ID retry reuses it
    rather than reselecting a default against a later balance.
    """
    operation_type = intent.operation_type
    view = cast("Payment", _FactsPayment(facts))
    validate_payment_amounts(view)

    if operation_type in {OperationType.PREPARE, OperationType.CANCEL_REFUND}:
        if intent.amount is not None:
            raise InvalidTransitionError(
                "This operation does not accept an amount."
            )
        if operation_type is OperationType.PREPARE and (
            facts.status != PaymentStatus.NEW
            or facts.captured_funds != 0
            or facts.remaining_authorization != 0
        ):
            raise InvalidTransitionError("Only a new payment can be prepared.")
        return None

    if operation_type is OperationType.RELEASE_LOCK:
        available = facts.remaining_authorization
        validate_amount(available, "Remaining authorization", allow_zero=False)
        if intent.amount is not None and intent.amount != available:
            raise InvalidTransitionError(
                "Authorization release covers the whole remaining "
                f"authorization {available}, not {intent.amount}."
            )
        return available

    # The eligibility rule and the amount bounds live in the state
    # engine, so a reservation and a ``PaymentFlow`` command cannot drift
    # apart. Running them here, at reservation time, is what refuses an
    # ineligible capture *before* submission rather than after the
    # provider has moved the money.
    if operation_type is OperationType.CHARGE:
        require_capture_eligible(view)
        available, name = capturable_amount(view), "Charge amount"
    else:
        available, name = refundable_amount(view), "Refund amount"

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
    target = intent.parameters.get(CANCELLATION_TARGET)
    for record in operations:
        if not record.is_active:
            continue
        if record.operation_id == intent.operation_id:
            continue
        if (
            intent.operation_type is OperationType.CANCEL_REFUND
            and record.operation_id == target
            and record.operation_type is OperationType.START_REFUND
            and record.state is OperationState.PROVIDER_PENDING
        ):
            continue
        return record
    return None


def _require_cancellation_target(
    operations: Iterable[OperationRecord], intent: OperationIntent
) -> OperationRecord:
    """Require a specific provider-pending refund with useful correlation."""
    target = intent.parameters.get(CANCELLATION_TARGET)
    for record in operations:
        if (
            record.operation_id == target
            and record.operation_type is OperationType.START_REFUND
            and record.state is OperationState.PROVIDER_PENDING
            and isinstance(record.correlation, str)
            and record.correlation.strip()
        ):
            return record
    raise OperationConflictError(
        "A refund cancellation must name the correlated provider-pending "
        f"refund through parameters[{CANCELLATION_TARGET!r}]; "
        f"{target!r} is not a cancellable refund.",
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

    A payment carrying a reconciliation requirement takes no *new*
    command at all: an ambiguous migrated record, a legacy operation that
    was already pending, or contradictory evidence must be settled by the
    application before more money moves. Already-reserved operations
    still resume and still resolve, so blocking never strands work that
    is already outstanding, and observations are unaffected -- callbacks
    and reconciliation continue while commands are blocked.

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
        return ReservationPlan(operation=record, created=False, facts=facts)

    if facts.reconciliation_required:
        raise ReconciliationBlockedError(
            f"Payment {facts.payment_id!r} requires reconciliation; "
            f"{intent.operation_id!r} cannot be reserved until the "
            "application has resolved it.",
            context={
                "payment_id": facts.payment_id,
                "operation_id": intent.operation_id,
            },
        )

    parameters = dict(intent.parameters)
    if intent.operation_type is OperationType.CANCEL_REFUND:
        target = _require_cancellation_target(operations, intent)
        parameters[CANCELLATION_CORRELATION] = target.correlation

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
            starting_authorization=facts.remaining_authorization,
            parameters=parameters,
            backend=facts.backend,
            reservation_sequence=max(
                (record.reservation_sequence for record in operations),
                default=0,
            )
            + 1,
        ),
        created=True,
        facts=(
            replace(facts, status=PaymentStatus.REFUND_STARTED)
            if intent.operation_type is OperationType.START_REFUND
            else facts
        ),
    )


def plan_submission(
    facts: PaymentFacts,
    operation: OperationRecord,
    *,
    expected_attempt: int,
    now: datetime,
    retry_until: datetime | None = None,
    idempotency_scope: str | None = None,
) -> SubmissionPlan:
    """Compare-and-set the submission counter without provider I/O.

    A retry caller MUST first reconcile and verify the provider's current
    idempotency declaration covers the frozen key, scope and payload. This
    local claim alone cannot establish that a provider retry is safe.
    Never extend the first attempt's timestamp or retry window on replay.
    """
    if operation.payment_id != facts.payment_id:
        raise OperationConflictError(
            "Operation belongs to a different payment."
        )
    if facts.reconciliation_required or operation.reconciliation_required:
        raise ReconciliationBlockedError(
            f"Payment {facts.payment_id!r} requires reconciliation "
            "before dispatch."
        )
    if type(expected_attempt) is not int or expected_attempt < 0:
        raise InvalidTransitionError(
            "Expected attempt must be a non-negative integer."
        )
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise InvalidTransitionError("Submission time must be timezone-aware.")
    if retry_until is not None and (
        not isinstance(retry_until, datetime) or retry_until.utcoffset() is None
    ):
        raise InvalidTransitionError("Retry deadline must be timezone-aware.")
    if operation.submission_attempts != expected_attempt:
        return SubmissionPlan(operation, granted=False)
    if operation.submission_attempts == 0:
        if operation.state is not OperationState.RESERVED:
            return SubmissionPlan(operation, granted=False)
        return SubmissionPlan(
            replace(
                operation,
                state=OperationState.SUBMITTING,
                submitted_at=now,
                submission_attempts=1,
                retry_until=retry_until,
                idempotency_scope=idempotency_scope,
            ),
            granted=True,
        )
    if (
        operation.state
        not in {OperationState.UNKNOWN, OperationState.SUBMITTING}
        or operation.retry_until is None
        or now >= operation.retry_until
    ):
        return SubmissionPlan(operation, granted=False)
    return SubmissionPlan(
        replace(
            operation,
            state=OperationState.SUBMITTING,
            submission_attempts=operation.submission_attempts + 1,
        ),
        granted=True,
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
        return PaymentUpdate(
            payment_event=PaymentEvent.PREPARED, external_id=outcome.external_id
        )
    if operation_type is OperationType.RELEASE_LOCK:
        return PaymentUpdate(payment_event=PaymentEvent.LOCK_RELEASED)
    if operation_type is OperationType.CANCEL_REFUND:
        return PaymentUpdate(payment_event=PaymentEvent.REFUND_CANCELLED)

    if settled is None:
        raise InvalidTransitionError(
            f"A succeeded {operation_type.value} needs a settled amount."
        )
    validate_amount(
        settled,
        f"{operation_type.value} settled amount",
        allow_zero=False,
        maximum=operation.resolved_amount,
        maximum_name="reserved amount",
    )

    if operation_type is OperationType.CHARGE:
        return PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=operation.starting_captured + settled,
        )
    return PaymentUpdate(
        payment_event=PaymentEvent.REFUND_CONFIRMED,
        refunded_amount=operation.starting_refunded + settled,
    )


def _confirmed_refund_total(
    operation: OperationRecord,
    settled: Decimal,
    operations: Iterable[OperationRecord],
) -> Decimal:
    """Combine each reserved baseline with independently proven later refunds.

    An intent cannot have settled before its reservation existed. Thus each
    starting total plus confirmed refunds reserved at/after that point is a
    cumulative lower bound. Taking the greatest bound preserves historical
    observations between older and newer intents, without counting a callback
    twice. Do not add the result to current facts. The complete retained history
    and atomically assigned reservation sequences are required for this proof.
    """
    refunds = [
        record
        for record in operations
        if record.payment_id == operation.payment_id
        and record.operation_type is OperationType.START_REFUND
        and record.operation_id != operation.operation_id
    ]
    refunds.append(
        replace(
            operation, state=OperationState.SUCCEEDED, settled_amount=settled
        )
    )
    confirmed = Decimal("0")
    cumulative = Decimal("0")
    for record in sorted(
        refunds, key=lambda record: record.reservation_sequence, reverse=True
    ):
        if (
            record.state is OperationState.SUCCEEDED
            and record.settled_amount is not None
        ):
            confirmed += record.settled_amount
        cumulative = max(cumulative, record.starting_refunded + confirmed)
    return cumulative


def plan_outcome(
    facts: PaymentFacts,
    operation: OperationRecord,
    outcome: OperationOutcome,
    *,
    operations: Iterable[OperationRecord] = (),
) -> OutcomePlan:
    """Plan the atomic recording of an operation outcome.

    The operation record and the financial facts it settles are returned
    together so the adapter commits them in one boundary. A nonterminal
    outcome -- including ``UNKNOWN`` -- moves no money and leaves the
    operation discoverable as unresolved work. Late nonterminal evidence
    cannot downgrade a terminal operation; contradictory terminal evidence
    or correlation is retained in ``conflicting_outcomes`` and flags
    reconciliation without overwriting established facts. Load the complete
    retained ``operations`` in the same boundary:
    cancellation success requires its target and returns it through
    ``related_operations``; confirmed refunds establish a cumulative lower
    bound even when cancellation let their reservation baselines overlap.
    """
    operations = tuple(operations)
    if type(outcome.reconciliation_required) is not bool:
        raise InvalidTransitionError(
            "Outcome reconciliation_required must be a boolean."
        )
    for name in ("correlation", "external_id"):
        value = getattr(outcome, name)
        if value is not None and (
            not isinstance(value, str) or not value.strip()
        ):
            raise InvalidTransitionError(
                f"Outcome {name} must be a nonempty string."
            )
    if (
        outcome.external_id is not None
        and operation.operation_type is not OperationType.PREPARE
    ):
        raise InvalidTransitionError(
            "Only preparation supplies a payment external ID."
        )
    if outcome.state not in {
        OperationState.SUCCEEDED,
        OperationState.REJECTED,
        OperationState.PROVIDER_PENDING,
        OperationState.UNKNOWN,
    }:
        raise InvalidTransitionError(
            "An outcome must describe provider evidence."
        )
    if outcome.settled_amount is not None and (
        outcome.state is not OperationState.SUCCEEDED
        or operation.operation_type
        not in {OperationType.CHARGE, OperationType.START_REFUND}
    ):
        raise InvalidTransitionError(
            "Only a confirmed capture or refund carries a settled amount."
        )
    if outcome.state is OperationState.SUCCEEDED:
        _settlement_update(operation, outcome)
    current = operation.state
    settled = (
        operation.resolved_amount
        if outcome.settled_amount is None
        else outcome.settled_amount
    )
    conflicting_correlation = (
        operation.correlation is not None
        and outcome.correlation is not None
        and operation.correlation != outcome.correlation
    )
    contradictory_terminal = (
        current in TERMINAL_OPERATION_STATES
        and outcome.state in TERMINAL_OPERATION_STATES
        and (
            current is not outcome.state
            or (
                current is OperationState.SUCCEEDED
                and settled != operation.settled_amount
            )
        )
    )
    conflicting_external_id = (
        facts.external_id is not None
        and outcome.external_id is not None
        and facts.external_id != outcome.external_id
    )
    reconciliation = (
        operation.reconciliation_required
        or outcome.reconciliation_required
        or conflicting_correlation
        or conflicting_external_id
        or contradictory_terminal
    )
    conflicts = operation.conflicting_outcomes
    if (
        conflicting_correlation
        or conflicting_external_id
        or contradictory_terminal
    ):
        # Store only normalized fields, never arbitrary provider payloads or
        # additional attributes on a plugin's outcome subclass.
        evidence = OperationOutcome(
            state=outcome.state,
            settled_amount=outcome.settled_amount,
            correlation=outcome.correlation,
            reconciliation_required=outcome.reconciliation_required,
            external_id=outcome.external_id,
        )
        if evidence not in conflicts:
            conflicts = (*conflicts, evidence)
    recorded = replace(
        operation,
        correlation=operation.correlation or outcome.correlation,
        reconciliation_required=reconciliation,
        conflicting_outcomes=conflicts,
    )
    facts = replace(
        facts,
        reconciliation_required=facts.reconciliation_required or reconciliation,
    )
    if conflicting_correlation or conflicting_external_id:
        return OutcomePlan(operation=recorded, facts=facts)
    if outcome.external_id is not None:
        facts = replace(facts, external_id=outcome.external_id)
    cancelled_refund_settled = (
        current is OperationState.REJECTED
        and outcome.state is OperationState.SUCCEEDED
        and operation.operation_type is OperationType.START_REFUND
        and any(
            candidate.operation_type is OperationType.CANCEL_REFUND
            and candidate.state is OperationState.SUCCEEDED
            and candidate.payment_id == operation.payment_id
            and candidate.parameters.get(CANCELLATION_TARGET)
            == operation.operation_id
            for candidate in operations
        )
    )
    if current in TERMINAL_OPERATION_STATES and not cancelled_refund_settled:
        return OutcomePlan(operation=recorded, facts=facts)
    # A cancellation can only stop the unexecuted part. A later correlated
    # settlement proves returned funds even if the cancellation arrived first.
    # Preserve the contradiction flag and record that financial fact.

    related: tuple[OperationRecord, ...] = ()
    if outcome.state is OperationState.SUCCEEDED:
        update = _settlement_update(operation, outcome)
        if operation.operation_type is OperationType.START_REFUND:
            assert settled is not None  # validated by _settlement_update
            update = replace(
                update,
                refunded_amount=_confirmed_refund_total(
                    operation, settled, operations
                ),
            )
        if operation.operation_type is OperationType.CANCEL_REFUND:
            target_id = operation.parameters.get(CANCELLATION_TARGET)
            target = next(
                (
                    record
                    for record in operations
                    if record.operation_id == target_id
                    and record.operation_type is OperationType.START_REFUND
                    and record.payment_id == facts.payment_id
                ),
                None,
            )
            if target is None:
                raise OperationConflictError(
                    "Cancellation target must be loaded atomically."
                )
            if target.is_active:
                related = (replace(target, state=OperationState.REJECTED),)
            update = replace(update, payment_event=None)
        elif (
            operation.operation_type is OperationType.RELEASE_LOCK
            and facts.remaining_authorization == 0
        ) or (
            operation.operation_type is OperationType.PREPARE
            and facts.status != PaymentStatus.NEW
        ):
            update = replace(update, payment_event=None)
        facts = _apply_to_facts(facts, update)
        recorded = replace(recorded, settled_amount=settled)

    next_state = outcome.state
    if (
        current is OperationState.PROVIDER_PENDING
        and next_state is OperationState.UNKNOWN
    ):
        next_state = current
    recorded = replace(recorded, state=next_state)
    if operation.operation_type in {
        OperationType.START_REFUND,
        OperationType.CANCEL_REFUND,
    }:
        replacements = {
            record.operation_id: record for record in (recorded, *related)
        }
        current_operations = [
            replacements.get(record.operation_id, record)
            for record in operations
        ]
        current_operations.append(recorded)
        refund_in_progress = any(
            record.operation_type is OperationType.START_REFUND
            and record.is_active
            for record in current_operations
        )
        facts = replace(
            facts,
            status=project_payment_status(
                cast("Payment", _FactsPayment(facts)),
                refund_in_progress=refund_in_progress,
            ),
        )
    return OutcomePlan(
        operation=recorded, facts=facts, related_operations=related
    )
