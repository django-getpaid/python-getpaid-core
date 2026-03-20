"""Tests for getpaid_core.types."""

from decimal import Decimal

from getpaid_core.enums import BackendMethod
from getpaid_core.enums import PaymentEvent
from getpaid_core.types import BuyerInfo
from getpaid_core.types import ChargeResult
from getpaid_core.types import ItemInfo
from getpaid_core.types import PaymentUpdate
from getpaid_core.types import RefundResult
from getpaid_core.types import TransactionResult


class TestBuyerInfo:
    def test_create_partial(self) -> None:
        info: BuyerInfo = {"email": "test@example.com"}
        assert info["email"] == "test@example.com"


class TestItemInfo:
    def test_create(self) -> None:
        item: ItemInfo = {
            "name": "Widget",
            "quantity": 2,
            "unit_price": Decimal("9.99"),
        }
        assert item["unit_price"] == Decimal("9.99")


class TestTransactionResult:
    def test_defaults(self) -> None:
        result = TransactionResult(method=BackendMethod.GET)
        assert result.method is BackendMethod.GET
        assert result.redirect_url is None
        assert result.headers == {}
        assert result.provider_data == {}

    def test_stores_gateway_metadata(self) -> None:
        result = TransactionResult(
            method=BackendMethod.POST,
            redirect_url="https://pay.example.com/form",
            form_data={"token": "abc"},
            external_id="session-123",
            provider_data={"raw_status": "pending"},
        )
        assert result.external_id == "session-123"
        assert result.provider_data["raw_status"] == "pending"


class TestChargeResult:
    def test_defaults(self) -> None:
        result = ChargeResult(
            amount_charged=Decimal("10.00"),
            success=True,
        )
        assert result.async_call is False
        assert result.provider_data == {}


class TestRefundResult:
    def test_defaults(self) -> None:
        result = RefundResult(amount=Decimal("12.00"))
        assert result.amount == Decimal("12.00")
        assert result.provider_data == {}


class TestPaymentUpdate:
    def test_defaults(self) -> None:
        update = PaymentUpdate()
        assert update.payment_event is None
        assert update.fraud_event is None
        assert update.provider_data == {}

    def test_supports_absolute_amounts_and_provider_event(self) -> None:
        update = PaymentUpdate(
            payment_event=PaymentEvent.PAYMENT_CAPTURED,
            paid_amount=Decimal("50.00"),
            provider_event_id="evt-1",
            provider_data={"provider_status": "CONFIRMED"},
        )
        assert update.paid_amount == Decimal("50.00")
        assert update.provider_event_id == "evt-1"
        assert update.provider_data["provider_status"] == "CONFIRMED"
