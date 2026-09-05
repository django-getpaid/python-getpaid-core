"""Internal monetary preconditions shared by the flow and state engine."""

from decimal import Decimal

from getpaid_core.exceptions import InvalidTransitionError


def validate_amount(
    amount: Decimal,
    name: str,
    *,
    allow_zero: bool = True,
    maximum: Decimal | None = None,
    maximum_name: str = "available amount",
) -> None:
    """Reject invalid money before comparing it or doing arithmetic."""
    if not isinstance(amount, Decimal) or not amount.is_finite():
        raise InvalidTransitionError(f"{name} must be a finite Decimal.")
    if amount < 0 or (not allow_zero and amount == 0):
        semantics = "non-negative" if allow_zero else "positive"
        raise InvalidTransitionError(f"{name} must be {semantics}.")
    if maximum is not None and amount > maximum:
        raise InvalidTransitionError(
            f"{name} {amount} exceeds {maximum_name} {maximum}."
        )
