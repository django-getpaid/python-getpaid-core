"""Tests for getpaid_core.registry.PluginRegistry."""

from unittest.mock import patch

import pytest

from getpaid_core.processor import BaseProcessor
from getpaid_core.registry import ENTRY_POINT_GROUP
from getpaid_core.registry import PluginRegistry
from getpaid_core.types import TransactionResult


# -- Test processors --


class PLNProcessor(BaseProcessor):
    slug = "pln-pay"
    display_name = "PLN Payments"
    accepted_currencies = ["PLN"]

    async def prepare_transaction(self, **kwargs):
        return TransactionResult(
            redirect_url=None,
            form_data=None,
            method="REST",
            headers={},
        )


class MultiProcessor(BaseProcessor):
    slug = "multi-pay"
    display_name = "Multi Payments"
    accepted_currencies = ["PLN", "EUR", "USD"]

    async def prepare_transaction(self, **kwargs):
        return TransactionResult(
            redirect_url=None,
            form_data=None,
            method="REST",
            headers={},
        )


class EURProcessor(BaseProcessor):
    slug = "eur-pay"
    display_name = "EUR Payments"
    accepted_currencies = ["EUR"]

    async def prepare_transaction(self, **kwargs):
        return TransactionResult(
            redirect_url=None,
            form_data=None,
            method="REST",
            headers={},
        )


# -- Tests --


class TestManualRegistration:
    def test_register(self):
        reg = PluginRegistry()
        reg.register(PLNProcessor)
        assert reg.get_by_slug("pln-pay") is PLNProcessor

    def test_register_multiple(self):
        reg = PluginRegistry()
        reg.register(PLNProcessor)
        reg.register(EURProcessor)
        assert reg.get_by_slug("pln-pay") is PLNProcessor
        assert reg.get_by_slug("eur-pay") is EURProcessor

    def test_unregister(self):
        reg = PluginRegistry()
        reg.register(PLNProcessor)
        reg.unregister("pln-pay")
        with pytest.raises(KeyError):
            reg.get_by_slug("pln-pay")

    def test_unregister_nonexistent_silent(self):
        reg = PluginRegistry()
        reg.unregister("nonexistent")  # should not raise


class TestGetBySlug:
    def test_unknown_slug_raises(self):
        reg = PluginRegistry()
        reg._discovered = True  # skip entry_point discovery
        with pytest.raises(KeyError):
            reg.get_by_slug("nonexistent")


class TestGetForCurrency:
    def test_single_match(self):
        reg = PluginRegistry()
        reg._discovered = True
        reg.register(PLNProcessor)
        reg.register(EURProcessor)
        result = reg.get_for_currency("PLN")
        assert result == [PLNProcessor]

    def test_multiple_matches(self):
        reg = PluginRegistry()
        reg._discovered = True
        reg.register(PLNProcessor)
        reg.register(MultiProcessor)
        result = reg.get_for_currency("PLN")
        assert set(result) == {PLNProcessor, MultiProcessor}

    def test_no_matches(self):
        reg = PluginRegistry()
        reg._discovered = True
        reg.register(PLNProcessor)
        result = reg.get_for_currency("GBP")
        assert result == []


class TestGetChoices:
    def test_returns_tuples(self):
        reg = PluginRegistry()
        reg._discovered = True
        reg.register(PLNProcessor)
        choices = reg.get_choices("PLN")
        assert choices == [("pln-pay", "PLN Payments")]

    def test_empty_for_unknown_currency(self):
        reg = PluginRegistry()
        reg._discovered = True
        choices = reg.get_choices("GBP")
        assert choices == []


class TestGetAllCurrencies:
    def test_union_of_all(self):
        reg = PluginRegistry()
        reg._discovered = True
        reg.register(PLNProcessor)
        reg.register(EURProcessor)
        reg.register(MultiProcessor)
        currencies = reg.get_all_currencies()
        assert currencies == {"PLN", "EUR", "USD"}


class TestEntryPointDiscovery:
    def test_entry_point_group_constant(self):
        assert ENTRY_POINT_GROUP == "getpaid.backends"

    def test_auto_discover_on_first_access(self):
        """Registry auto-discovers on first query if not yet done."""
        reg = PluginRegistry()
        assert reg._discovered is False
        # Calling get_for_currency triggers discovery
        with patch.object(reg, "discover") as mock_discover:
            reg.get_for_currency("PLN")
            mock_discover.assert_called_once()

    def test_manual_register_skips_discovery(self):
        """Manual register does not trigger entry_point discovery."""
        reg = PluginRegistry()
        reg.register(PLNProcessor)
        # _discovered is still False after manual register
        assert reg._discovered is False


class TestSingleton:
    def test_module_level_registry_exists(self):
        from getpaid_core.registry import registry

        assert isinstance(registry, PluginRegistry)
