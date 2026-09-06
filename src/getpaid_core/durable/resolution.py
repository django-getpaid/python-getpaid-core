"""Audited, application-driven resolution; never a provider command."""

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING
from typing import cast

from getpaid_core._amounts import validate_amount
from getpaid_core._amounts import validate_payment_amounts
from getpaid_core.durable.evidence import normalize_outcome
from getpaid_core.durable.records import TERMINAL_OPERATION_STATES
from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationRecord
from getpaid_core.durable.records import OperationState
from getpaid_core.durable.records import OutcomePlan
from getpaid_core.durable.records import PaymentFacts
from getpaid_core.durable.rules import _FactsPayment
from getpaid_core.durable.rules import plan_outcome
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import OperationConflictError
from getpaid_core.exceptions import StateConflictError


if TYPE_CHECKING:
    from getpaid_core.protocols import Payment


def _audit_text(value: object) -> bool:
    return (
        type(value) is str
        and bool(value.strip())
        and len(value) <= 2000
        and all(character.isprintable() for character in value)
    )


@dataclass(frozen=True, slots=True)
class OperatorResolution:
    """An operator's evidenced decision, authorized by the application.

    ``resolution_id`` is stable for acknowledgement retries. References point
    to controlled evidence, not embedded provider payloads. ``resolved_at``
    records the decision time; it never establishes a provider outcome.
    Clearing payment-wide reconciliation is explicit, covers the reviewed
    facts, and cannot dismiss another operation's unresolved dispute.
    """

    resolution_id: str
    actor: str
    reason: str
    evidence_references: tuple[str, ...]
    resolved_at: datetime
    outcome: OperationOutcome
    clear_payment_reconciliation: bool = False

    def __post_init__(self) -> None:
        if not all(
            _audit_text(value)
            for value in (self.resolution_id, self.actor, self.reason)
        ):
            raise InvalidTransitionError(
                "Resolution needs ID, actor and reason."
            )
        if (
            not isinstance(self.evidence_references, (tuple, list))
            or not self.evidence_references
            or not all(_audit_text(value) for value in self.evidence_references)
        ):
            raise InvalidTransitionError(
                "Resolution needs evidence references."
            )
        object.__setattr__(
            self, "evidence_references", tuple(self.evidence_references)
        )
        if (
            not isinstance(self.resolved_at, datetime)
            or self.resolved_at.tzinfo is None
            or self.resolved_at.utcoffset() is None
        ):
            raise InvalidTransitionError(
                "Resolution time must be timezone-aware."
            )
        if type(self.clear_payment_reconciliation) is not bool:
            raise InvalidTransitionError(
                "Reconciliation acknowledgement must be boolean."
            )
        if not isinstance(self.outcome, OperationOutcome):
            raise InvalidTransitionError(
                "Resolution needs a normalized outcome."
            )
        outcome = normalize_outcome(self.outcome)
        if (
            outcome.state not in TERMINAL_OPERATION_STATES
            or outcome.reconciliation_required is not False
        ):
            raise InvalidTransitionError(
                "Resolution must establish a terminal effect."
            )
        object.__setattr__(self, "outcome", outcome)


def plan_resolution(
    facts: PaymentFacts,
    operation: OperationRecord,
    resolution: OperatorResolution,
    *,
    expected_facts: PaymentFacts,
    expected_operation: OperationRecord,
    operations: Iterable[OperationRecord],
) -> OutcomePlan:
    """Compare reviewed snapshots and commit evidence, audit and money together.

    Prior disputes/recovery claims are retained. A subsequent callback can
    dispute this decision again; the audit never suppresses new evidence.
    No decision can undo confirmed captured/refunded funds.
    """
    if type(resolution) is not OperatorResolution:
        raise InvalidTransitionError("Resolution must be an audited decision.")
    for previous in operation.resolutions:
        if previous.resolution_id == resolution.resolution_id:
            if previous != resolution:
                raise OperationConflictError(
                    "Resolution ID already binds another decision."
                )
            return OutcomePlan(operation, facts)
    if facts != expected_facts or operation != expected_operation:
        raise StateConflictError(
            "Reconciliation evidence changed; review current state."
        )
    if operation.payment_id != facts.payment_id:
        raise OperationConflictError(
            "Resolution payment identity does not match."
        )
    validate_payment_amounts(cast("Payment", _FactsPayment(facts)))
    outcome = resolution.outcome
    settled = (
        operation.resolved_amount
        if outcome.settled_amount is None
        else outcome.settled_amount
    )
    correcting_settlement = False
    if operation.state is OperationState.SUCCEEDED:
        if outcome.state is not OperationState.SUCCEEDED:
            raise InvalidTransitionError(
                "Resolution cannot undo confirmed effects."
            )
        if settled != operation.settled_amount:
            if settled is None or operation.settled_amount is None:
                raise InvalidTransitionError(
                    "Resolution cannot change an unquantified effect."
                )
            validate_amount(
                settled,
                "Corrected settlement",
                allow_zero=False,
                maximum=operation.resolved_amount,
            )
            if settled < operation.settled_amount:
                raise InvalidTransitionError(
                    "Resolution cannot undo confirmed effects."
                )
            correcting_settlement = True
    operations = tuple(operations)
    if (
        correcting_settlement
        or (
            operation.state is OperationState.REJECTED
            and outcome.state is OperationState.SUCCEEDED
        )
    ) and any(
        entry.reservation_sequence > operation.reservation_sequence
        for entry in operations
    ):
        raise InvalidTransitionError(
            "Cannot change settlement after later intents; reconcile "
            "cumulative provider evidence without reusing an old baseline."
        )
    conflicts = operation.conflicting_outcomes
    if correcting_settlement:
        previous = OperationOutcome(
            OperationState.SUCCEEDED,
            settled_amount=operation.settled_amount,
            correlation=operation.correlation,
        )
        if previous not in conflicts:
            conflicts = (*conflicts, previous)
    candidate = replace(
        operation,
        reconciliation_required=False,
        conflicting_outcomes=conflicts,
        # Only the audited path may revisit a terminal settlement. The normal
        # outcome planner validates the corrected total against its reserved
        # baseline and current facts, rather than adding the amount again.
        state=(
            OperationState.UNKNOWN
            if operation.state is OperationState.REJECTED
            or correcting_settlement
            else operation.state
        ),
    )
    plan = plan_outcome(facts, candidate, outcome, operations=operations)
    if plan.operation.reconciliation_required:
        raise InvalidTransitionError(
            "Resolution conflicts with financial facts or correlation."
        )
    if (
        operation.state in TERMINAL_OPERATION_STATES
        and facts.status == PaymentStatus.REFUND_STARTED
        and not plan.related_operations
    ):
        # Without a still-active cancellation target completed by this plan,
        # a terminal intent no longer owns the pending-refund marker.
        # Reconfirming or correcting it cannot resolve unrelated external
        # work, which may have no local reservation in the retained history.
        plan = replace(
            plan, facts=replace(plan.facts, status=PaymentStatus.REFUND_STARTED)
        )
    recorded = replace(
        plan.operation,
        resolutions=(*operation.resolutions, resolution),
        pending_response_attempts=(),
    )
    facts = plan.facts
    if resolution.clear_payment_reconciliation:
        replacements = {
            entry.operation_id: entry
            for entry in (recorded, *plan.related_operations)
        }
        if any(
            replacements.get(entry.operation_id, entry).reconciliation_required
            for entry in operations
        ):
            raise OperationConflictError(
                "Other operation disputes still require reconciliation."
            )
        facts = replace(facts, reconciliation_required=False)
    return OutcomePlan(recorded, facts, plan.related_operations)
