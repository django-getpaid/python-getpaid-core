"""Protocols defining framework integration contracts."""

from decimal import Decimal
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from getpaid_core.types import BuyerInfo
from getpaid_core.types import ItemInfo


@runtime_checkable
class Order(Protocol):
    """What the core expects from an order object."""

    def get_total_amount(self) -> Decimal: ...
    def get_buyer_info(self) -> BuyerInfo: ...
    def get_description(self) -> str: ...
    def get_currency(self) -> str: ...
    def get_items(self) -> list[ItemInfo]: ...
    def get_return_url(self, success: bool | None = None) -> str: ...


@runtime_checkable
class Payment(Protocol):
    """What the core expects from a payment object."""

    id: str
    order: Order
    amount_required: Decimal
    currency: str
    status: str
    backend: str
    external_id: str | None
    description: str | None
    amount_paid: Decimal
    amount_locked: Decimal
    amount_refunded: Decimal
    fraud_status: str
    fraud_message: str
    provider_data: dict[str, Any]


@runtime_checkable
class PaymentRepository(Protocol):
    """Persistence abstraction. Framework adapters implement this."""

    async def get_by_id(self, payment_id: str) -> Payment: ...
    async def create(self, **kwargs) -> Payment: ...
    async def save(self, payment: Payment) -> Payment: ...
    async def update_status(
        self, payment_id: str, status: str, **fields
    ) -> Payment: ...
    async def list_by_order(self, order_id: str) -> list[Payment]: ...
