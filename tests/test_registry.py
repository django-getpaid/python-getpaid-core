"""Tests for getpaid_core.registry.PluginRegistry."""

import threading
from collections.abc import Callable
from collections.abc import Iterator
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import ClassVar
from typing import TypeVar
from unittest.mock import patch

import pytest

from getpaid_core.enums import BackendMethod
from getpaid_core.exceptions import BackendNotFoundError
from getpaid_core.processor import BaseProcessor
from getpaid_core.registry import ENTRY_POINT_GROUP
from getpaid_core.registry import PluginRegistry
from getpaid_core.types import TransactionResult


_QueryResultT = TypeVar("_QueryResultT")


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


class EmptySlugProcessor(BaseProcessor):
    slug = ""
    display_name = "No Slug"

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        return TransactionResult(method=BackendMethod.REST)


class TestManualRegistration:
    def test_register_empty_slug_raises(self) -> None:
        registry = PluginRegistry()
        registry._discovered = True

        with pytest.raises(ValueError, match="empty slug"):
            registry.register(EmptySlugProcessor)

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

    def test_get_by_slug_unknown_raises_domain_error(self) -> None:
        """Unknown slugs raise BackendNotFoundError, which is both a
        GetPaidException and a KeyError (backwards compatibility)."""
        registry = PluginRegistry()
        registry._discovered = True

        with pytest.raises(BackendNotFoundError) as exc_info:
            registry.get_by_slug("nonexistent")

        assert isinstance(exc_info.value, KeyError)
        assert exc_info.value.context["slug"] == "nonexistent"
        assert "nonexistent" in str(exc_info.value)


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

    def test_non_processor_entry_point_logs_warning(self, caplog) -> None:
        """An entry point that loads something other than a BaseProcessor
        subclass is skipped with a warning, not silently."""
        invalid = SimpleNamespace(name="bad-plugin", load=lambda: object)
        valid = SimpleNamespace(name="good-plugin", load=lambda: PLNProcessor)

        registry = PluginRegistry()
        with (
            patch(
                "getpaid_core.registry.entry_points",
                return_value=[invalid, valid],
            ),
            caplog.at_level("WARNING", logger="getpaid_core.registry"),
        ):
            registry.discover()

        assert registry.get_by_slug("pln-pay") is PLNProcessor
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "bad-plugin" in warnings[0].getMessage()

    def test_broken_entry_point_does_not_abort_discovery(self, caplog) -> None:
        """One plugin failing to import must not prevent discovery of the
        remaining plugins."""

        def broken_load():
            raise ImportError("plugin is broken")

        broken = SimpleNamespace(name="broken-plugin", load=broken_load)
        valid = SimpleNamespace(name="good-plugin", load=lambda: PLNProcessor)

        registry = PluginRegistry()
        with (
            patch(
                "getpaid_core.registry.entry_points",
                return_value=[broken, valid],
            ),
            caplog.at_level("WARNING", logger="getpaid_core.registry"),
        ):
            registry.discover()

        assert registry.get_by_slug("pln-pay") is PLNProcessor
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "broken-plugin" in warnings[0].getMessage()


class TestThreadSafety:
    def test_ensure_discovered_concurrent_calls(self) -> None:
        """Multiple threads calling _ensure_discovered concurrently must
        only invoke discover() once."""
        registry = PluginRegistry()
        registry._discovered = False
        call_count = 0
        call_lock = threading.Lock()
        barrier = threading.Barrier(4)

        original_discover = registry.discover

        def counting_discover() -> None:
            nonlocal call_count
            with call_lock:
                call_count += 1
            original_discover()

        with (
            patch(
                "getpaid_core.registry.entry_points",
                return_value=[SimpleNamespace(load=lambda: PLNProcessor)],
            ),
            patch.object(registry, "discover", side_effect=counting_discover),
            ThreadPoolExecutor(max_workers=4) as pool,
        ):

            def worker() -> None:
                barrier.wait()  # synchronise all threads
                registry._ensure_discovered()

            futures = [pool.submit(worker) for _ in range(4)]
            for f in futures:
                f.result()

        # discover() should have been called exactly once
        assert call_count == 1

    def test_concurrent_register_unregister(self) -> None:
        """register/unregister take the registry lock; concurrent use
        must neither lose registrations nor raise."""
        registry = PluginRegistry()
        registry._discovered = True

        processors = []
        for i in range(16):
            processors.append(
                type(
                    f"Proc{i}",
                    (PLNProcessor,),
                    {"slug": f"proc-{i}"},
                )
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(registry.register, proc) for proc in processors
            ]
            for f in futures:
                f.result()

        for i, proc in enumerate(processors):
            assert registry.get_by_slug(f"proc-{i}") is proc

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(registry.unregister, f"proc-{i}") for i in range(16)
            ]
            for f in futures:
                f.result()

        for i in range(16):
            with pytest.raises(BackendNotFoundError):
                registry.get_by_slug(f"proc-{i}")

    def test_concurrent_get_by_slug(self) -> None:
        """Multiple threads calling get_by_slug concurrently must not
        raise errors or return inconsistent results."""
        registry = PluginRegistry()
        registry._discovered = False

        with (
            patch(
                "getpaid_core.registry.entry_points",
                return_value=[SimpleNamespace(load=lambda: PLNProcessor)],
            ),
            ThreadPoolExecutor(max_workers=8) as pool,
        ):
            futures = [
                pool.submit(registry.get_by_slug, "pln-pay") for _ in range(32)
            ]
            for f in futures:
                result = f.result()
                assert result is PLNProcessor


OVERLAP_TIMEOUT_SECONDS = 5


class PausingCurrencies(Sequence[str]):
    """A plugin-owned currency sequence that blocks the reading thread.

    It only controls scheduling so a query can be caught mid-iteration;
    it never touches the registry itself.
    """

    def __init__(
        self,
        values: Sequence[str],
        entered: threading.Event,
        resume: threading.Event,
    ) -> None:
        self._values = tuple(values)
        self.entered = entered
        self.resume = resume

    def _pause(self) -> None:
        self.entered.set()
        if not self.resume.wait(timeout=OVERLAP_TIMEOUT_SECONDS):
            raise TimeoutError("The writer never released the reader.")

    def __len__(self) -> int:
        return len(self._values)

    def __getitem__(self, index):
        return self._values[index]

    def __contains__(self, value: object) -> bool:
        self._pause()
        return value in self._values

    def __iter__(self) -> Iterator[str]:
        self._pause()
        return iter(self._values)


class TestQueryWriteOverlap:
    """Queries iterate a snapshot taken under the registry lock, so a
    concurrent register/unregister can neither break the iteration nor
    change the result of a query already in progress."""

    @staticmethod
    def _pausing_registry() -> tuple[
        PluginRegistry, threading.Event, threading.Event
    ]:
        entered = threading.Event()
        resume = threading.Event()

        class PausingProcessor(PLNProcessor):
            slug = "pausing-pay"
            display_name = "Pausing Payments"
            accepted_currencies = PausingCurrencies(["PLN"], entered, resume)

        registry = PluginRegistry()
        registry._discovered = True
        registry.register(PausingProcessor)
        registry.register(EURProcessor)
        return registry, entered, resume

    @staticmethod
    def _run_overlapped(
        query: Callable[[], _QueryResultT],
        mutate: Callable[[], None],
        entered: threading.Event,
        resume: threading.Event,
    ) -> _QueryResultT:
        """Start ``query``, run ``mutate`` while it is paused mid-query,
        then let the query finish and return its result."""
        with ThreadPoolExecutor(max_workers=1) as pool:
            reader = pool.submit(query)
            try:
                assert entered.wait(timeout=OVERLAP_TIMEOUT_SECONDS), (
                    "query never paused"
                )
                mutate()
            finally:
                resume.set()
            return reader.result(timeout=OVERLAP_TIMEOUT_SECONDS)

    def test_get_for_currency_survives_concurrent_unregister(self) -> None:
        registry, entered, resume = self._pausing_registry()
        registry.register(
            type("OtherPLN", (PLNProcessor,), {"slug": "other-pln"})
        )

        backends = self._run_overlapped(
            lambda: registry.get_for_currency("PLN"),
            lambda: registry.unregister("other-pln"),
            entered,
            resume,
        )

        slugs = [backend.slug for backend in backends]
        assert slugs == ["pausing-pay", "other-pln"]

    def test_get_for_currency_survives_concurrent_register(self) -> None:
        registry, entered, resume = self._pausing_registry()
        late = type("LatePLN", (PLNProcessor,), {"slug": "late-pln"})

        backends = self._run_overlapped(
            lambda: registry.get_for_currency("PLN"),
            lambda: registry.register(late),
            entered,
            resume,
        )

        assert [backend.slug for backend in backends] == ["pausing-pay"]
        assert registry.get_by_slug("late-pln") is late

    def test_get_choices_survives_concurrent_unregister(self) -> None:
        registry, entered, resume = self._pausing_registry()
        registry.register(
            type(
                "OtherPLN",
                (PLNProcessor,),
                {"slug": "other-pln", "display_name": "Other PLN"},
            )
        )

        choices = self._run_overlapped(
            lambda: registry.get_choices("PLN"),
            lambda: registry.unregister("other-pln"),
            entered,
            resume,
        )

        assert choices == [
            ("pausing-pay", "Pausing Payments"),
            ("other-pln", "Other PLN"),
        ]

    def test_get_choices_survives_concurrent_register(self) -> None:
        registry, entered, resume = self._pausing_registry()
        late = type("LatePLN", (PLNProcessor,), {"slug": "late-pln"})

        choices = self._run_overlapped(
            lambda: registry.get_choices("PLN"),
            lambda: registry.register(late),
            entered,
            resume,
        )

        assert choices == [("pausing-pay", "Pausing Payments")]

    def test_get_all_currencies_survives_concurrent_unregister(self) -> None:
        registry, entered, resume = self._pausing_registry()

        currencies = self._run_overlapped(
            registry.get_all_currencies,
            lambda: registry.unregister("eur-pay"),
            entered,
            resume,
        )

        assert currencies == {"PLN", "EUR"}

    def test_get_all_currencies_survives_concurrent_register(self) -> None:
        registry, entered, resume = self._pausing_registry()
        late = type(
            "LateUSD",
            (PLNProcessor,),
            {"slug": "late-usd", "accepted_currencies": ("USD",)},
        )

        currencies = self._run_overlapped(
            registry.get_all_currencies,
            lambda: registry.register(late),
            entered,
            resume,
        )

        assert currencies == {"PLN", "EUR"}
