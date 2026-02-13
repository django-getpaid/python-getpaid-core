"""Core type definitions for payment processing."""

from decimal import Decimal
from typing import TypedDict


class BuyerInfo(TypedDict, total=False):
    """Buyer/customer information."""

    email: str
    first_name: str
    last_name: str
    phone: str


class ItemInfo(TypedDict):
    """Single item in an order."""

    name: str
    quantity: int
    unit_price: Decimal


class ChargeResponse(TypedDict):
    """Response from charging a pre-authorized payment."""

    amount_charged: Decimal
    success: bool
    async_call: bool


class PaymentStatusResponse(TypedDict, total=False):
    """Response from fetching payment status from gateway."""

    amount: Decimal | None
    status: str | None
    external_id: str | None


class TransactionResult(TypedDict):
    """Result of preparing a transaction.

    Framework adapters convert this into framework-specific responses:
    - GET: redirect to redirect_url
    - POST: render form that auto-submits to redirect_url with form_data
    - REST: return JSON or handle internally
    """

    redirect_url: str | None
    form_data: dict | None
    method: str  # 'GET', 'POST', or 'REST'
    headers: dict[str, str]
