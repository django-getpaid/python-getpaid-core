"""Internal monetary preconditions shared by the flow and state engine."""

from decimal import Decimal
from typing import TYPE_CHECKING

from getpaid_core.exceptions import InvalidTransitionError


if TYPE_CHECKING:
    from getpaid_core.protocols import Payment


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


def validate_payment_amounts(payment: "Payment") -> None:
    """Reject unusable stored balances before deriving operation limits."""
    validate_amount(payment.amount_required, "amount_required")
    validate_amount(
        payment.amount_paid,
        "amount_paid",
        maximum=payment.amount_required,
        maximum_name="amount_required",
    )
    validate_amount(
        payment.amount_locked,
        "amount_locked",
        maximum=payment.amount_required - payment.amount_paid,
        maximum_name="uncaptured amount_required",
    )
    validate_amount(
        payment.amount_refunded,
        "amount_refunded",
        maximum=payment.amount_paid,
        maximum_name="amount_paid",
    )
