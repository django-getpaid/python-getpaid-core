"""Core type definitions for payment processing."""

from dataclasses import dataclass
from dataclasses import field
from decimal import Decimal
from typing import Any
from typing import TypedDict

from getpaid_core.enums import BackendMethod
from getpaid_core.enums import FraudEvent
from getpaid_core.enums import PaymentEvent


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


@dataclass(slots=True)
class TransactionResult:
    """Result of preparing a transaction."""

    method: BackendMethod | str
    redirect_url: str | None = None
    form_data: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    external_id: str | None = None
    provider_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.method = BackendMethod(self.method)
        self.headers = dict(self.headers)
        self.form_data = (
            None if self.form_data is None else dict(self.form_data)
        )
        self.provider_data = dict(self.provider_data or {})


@dataclass(slots=True)
class ChargeResult:
    """Result of charging a pre-authorized payment.

    ``amount_charged``, ``success`` and ``async_call`` are core-owned
    fields and safe for diagnostics. ``provider_data`` is plugin-defined
    and may carry stored credentials, raw provider responses or buyer
    details: core never logs it, and neither should callers without
    redacting it first.
    """

    amount_charged: Decimal
    success: bool
    async_call: bool = False
    provider_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.provider_data = dict(self.provider_data or {})


@dataclass(slots=True)
class RefundResult:
    """Result of starting a refund."""

    amount: Decimal
    provider_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.provider_data = dict(self.provider_data or {})


@dataclass(slots=True)
class PaymentUpdate:
    """Semantic payment update returned by callbacks or status polling."""

    payment_event: PaymentEvent | str | None = None
    fraud_event: FraudEvent | str | None = None
    paid_amount: Decimal | None = None
    refunded_amount: Decimal | None = None
    locked_amount: Decimal | None = None
    external_id: str | None = None
    fraud_message: str | None = None
    provider_event_id: str | None = None
    provider_data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.payment_event is not None:
            self.payment_event = PaymentEvent(self.payment_event)
        if self.fraud_event is not None:
            self.fraud_event = FraudEvent(self.fraud_event)
        self.provider_data = dict(self.provider_data or {})
