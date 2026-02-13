"""Tests for getpaid_core.types."""

from decimal import Decimal

from getpaid_core.types import BuyerInfo
from getpaid_core.types import ChargeResponse
from getpaid_core.types import ItemInfo
from getpaid_core.types import PaymentStatusResponse
from getpaid_core.types import TransactionResult


class TestBuyerInfo:
    def test_create_full(self):
        info: BuyerInfo = {
            "email": "test@example.com",
            "first_name": "Jan",
            "last_name": "Kowalski",
            "phone": "+48123456789",
        }
        assert info["email"] == "test@example.com"

    def test_create_partial(self):
        """BuyerInfo has total=False, all fields optional."""
        info: BuyerInfo = {"email": "test@example.com"}
        assert info["email"] == "test@example.com"

    def test_empty_is_valid(self):
        info: BuyerInfo = {}
        assert isinstance(info, dict)


class TestItemInfo:
    def test_create(self):
        item: ItemInfo = {
            "name": "Widget",
            "quantity": 2,
            "unit_price": Decimal("9.99"),
        }
        assert item["name"] == "Widget"
        assert item["quantity"] == 2
        assert item["unit_price"] == Decimal("9.99")


class TestChargeResponse:
    def test_create(self):
        resp: ChargeResponse = {
            "amount_charged": Decimal("100.00"),
            "success": True,
            "async_call": False,
        }
        assert resp["success"] is True


class TestPaymentStatusResponse:
    def test_create_full(self):
        resp: PaymentStatusResponse = {
            "amount": Decimal("50.00"),
            "status": "paid",
            "external_id": "ext-123",
        }
        assert resp["status"] == "paid"

    def test_create_empty(self):
        """PaymentStatusResponse has total=False."""
        resp: PaymentStatusResponse = {}
        assert isinstance(resp, dict)


class TestTransactionResult:
    def test_redirect(self):
        result: TransactionResult = {
            "redirect_url": "https://pay.example.com/123",
            "form_data": None,
            "method": "GET",
            "headers": {},
        }
        assert result["method"] == "GET"
        assert result["redirect_url"] == "https://pay.example.com/123"

    def test_post_form(self):
        result: TransactionResult = {
            "redirect_url": "https://pay.example.com/form",
            "form_data": {"token": "abc", "amount": "100"},
            "method": "POST",
            "headers": {"X-Signature": "sig123"},
        }
        assert result["method"] == "POST"
        assert result["form_data"]["token"] == "abc"

    def test_rest(self):
        result: TransactionResult = {
            "redirect_url": None,
            "form_data": None,
            "method": "REST",
            "headers": {},
        }
        assert result["redirect_url"] is None
