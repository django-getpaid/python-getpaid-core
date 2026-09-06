"""Allowlisted recovery evidence; never serialize a plugin's result object."""

import re
from dataclasses import dataclass
from decimal import Decimal

from getpaid_core.durable.records import OperationOutcome
from getpaid_core.durable.records import OperationState
from getpaid_core.exceptions import InvalidTransitionError


_HANDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")


def safe_handle(value: object) -> str | None:
    """Accept bounded opaque identifiers, not URLs, payloads or control text.

    Plugins must select non-secret provider identifiers. Syntax cannot prove
    that an otherwise valid identifier is not a credential.
    """
    return value if type(value) is str and _HANDLE.fullmatch(value) else None


@dataclass(frozen=True, slots=True)
class RecoveryEvidence:
    """Safe response fields, including partial evidence on validation failure.

    Missing fields mean unavailable/unsafe, not rejection or remote rollback.
    Finite impossible money is retained as a claim, never as applied funds.
    """

    state: OperationState | None = None
    settled_amount: Decimal | None = None
    correlation: str | None = None
    external_id: str | None = None

    @classmethod
    def from_outcome(cls, outcome: object) -> "RecoveryEvidence":
        if not isinstance(outcome, OperationOutcome):
            return cls()
        amount = outcome.settled_amount
        return cls(
            state=outcome.state
            if type(outcome.state) is OperationState
            else None,
            settled_amount=(
                amount
                if type(amount) is Decimal and amount.is_finite()
                else None
            ),
            correlation=safe_handle(outcome.correlation),
            external_id=safe_handle(outcome.external_id),
        )


def normalize_outcome(outcome: OperationOutcome) -> OperationOutcome:
    """Copy declared fields; reject unsafe handles before persistence."""
    for name in ("correlation", "external_id"):
        value = getattr(outcome, name)
        if value is not None and safe_handle(value) is None:
            raise InvalidTransitionError(
                f"Outcome {name} must be a safe opaque identifier."
            )
    return OperationOutcome(
        state=outcome.state,
        settled_amount=outcome.settled_amount,
        correlation=outcome.correlation,
        external_id=outcome.external_id,
        reconciliation_required=outcome.reconciliation_required,
    )
