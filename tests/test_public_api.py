"""Tests for the public API surface of getpaid_core."""

import getpaid_core


class TestPublicAPI:
    def test_version(self) -> None:
        assert getpaid_core.__version__ == "3.0.0a2"

    def test_exports_enums(self) -> None:
        assert getpaid_core.PaymentStatus is not None
        assert getpaid_core.PaymentEvent is not None
        assert getpaid_core.FraudStatus is not None
        assert getpaid_core.FraudEvent is not None

    def test_exports_exceptions(self) -> None:
        assert getpaid_core.GetPaidException is not None
        assert getpaid_core.InvalidTransitionError is not None

    def test_exports_core_classes(self) -> None:
        assert getpaid_core.BaseProcessor is not None
        assert getpaid_core.PaymentFlow is not None

    def test_exports_registry(self) -> None:
        assert getpaid_core.registry is not None
