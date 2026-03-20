"""Tests for getpaid_core.registry.PluginRegistry."""

from collections.abc import Sequence
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch

import pytest

from getpaid_core.enums import BackendMethod
from getpaid_core.processor import BaseProcessor
from getpaid_core.registry import ENTRY_POINT_GROUP
from getpaid_core.registry import PluginRegistry
from getpaid_core.types import TransactionResult


class PLNProcessor(BaseProcessor):
    slug = "pln-pay"
    display_name = "PLN Payments"
    accepted_currencies: ClassVar[Sequence[str]] = ("PLN",)

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        return TransactionResult(method=BackendMethod.REST)


class EURProcessor(BaseProcessor):
    slug = "eur-pay"
    display_name = "EUR Payments"
    accepted_currencies: ClassVar[Sequence[str]] = ("EUR",)

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        return TransactionResult(method=BackendMethod.REST)


class DuplicatePLNProcessor(BaseProcessor):
    slug = "pln-pay"
    display_name = "Duplicate PLN"
    accepted_currencies: ClassVar[Sequence[str]] = ("PLN",)

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        return TransactionResult(method=BackendMethod.REST)


class TestManualRegistration:
    def test_register_duplicate_slug_raises(self) -> None:
        registry = PluginRegistry()
        registry._discovered = True
        registry.register(PLNProcessor)

        with pytest.raises(ValueError, match="Duplicate backend slug"):
            registry.register(DuplicatePLNProcessor)

    def test_unregister(self) -> None:
        registry = PluginRegistry()
        registry._discovered = True
        registry.register(PLNProcessor)
        registry.unregister("pln-pay")

        with pytest.raises(KeyError):
            registry.get_by_slug("pln-pay")


class TestQueries:
    def test_get_for_currency(self) -> None:
        registry = PluginRegistry()
        registry._discovered = True
        registry.register(PLNProcessor)
        registry.register(EURProcessor)

        assert registry.get_for_currency("PLN") == [PLNProcessor]

    def test_get_choices(self) -> None:
        registry = PluginRegistry()
        registry._discovered = True
        registry.register(PLNProcessor)

        assert registry.get_choices("PLN") == [("pln-pay", "PLN Payments")]

    def test_get_all_currencies(self) -> None:
        registry = PluginRegistry()
        registry._discovered = True
        registry.register(PLNProcessor)
        registry.register(EURProcessor)

        assert registry.get_all_currencies() == {"PLN", "EUR"}


class TestEntryPointDiscovery:
    def test_entry_point_group_constant(self) -> None:
        assert ENTRY_POINT_GROUP == "getpaid.backends"

    def test_loads_valid_entry_points(self) -> None:
        entry_point = SimpleNamespace(load=lambda: PLNProcessor)

        registry = PluginRegistry()
        with patch(
            "getpaid_core.registry.entry_points", return_value=[entry_point]
        ):
            registry.discover()

        assert registry.get_by_slug("pln-pay") is PLNProcessor

    def test_ignores_invalid_entry_points(self) -> None:
        invalid = SimpleNamespace(load=lambda: object)
        valid = SimpleNamespace(load=lambda: PLNProcessor)

        registry = PluginRegistry()
        with patch(
            "getpaid_core.registry.entry_points",
            return_value=[invalid, valid],
        ):
            registry.discover()

        assert registry.get_by_slug("pln-pay") is PLNProcessor
