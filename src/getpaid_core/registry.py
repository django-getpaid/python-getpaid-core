"""Plugin registry for payment backends."""

import logging
import threading
from importlib.metadata import entry_points

from getpaid_core.exceptions import BackendNotFoundError
from getpaid_core.processor import BaseProcessor


logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "getpaid.backends"


class PluginRegistry:
    """Discovers and stores payment backend processors."""

    def __init__(self) -> None:
        self._backends: dict[str, type[BaseProcessor]] = {}
        self._discovered = False
        # Re-entrant: discover() is also called while _ensure_discovered()
        # already holds the lock.
        self._lock = threading.RLock()

    def discover(self) -> None:
        """Load all backends registered via entry points.

        A plugin that fails to import, or that does not provide a
        ``BaseProcessor`` subclass, is skipped with a logged warning and
        does not abort discovery of the remaining plugins.
        """
        with self._lock:
            for entry_point in entry_points(group=ENTRY_POINT_GROUP):
                name = getattr(entry_point, "name", repr(entry_point))
                try:
                    processor_class = entry_point.load()
                except Exception:
                    logger.warning(
                        "Failed to load payment backend entry point %r "
                        "from group %r; skipping it.",
                        name,
                        ENTRY_POINT_GROUP,
                        exc_info=True,
                    )
                    continue
                if isinstance(processor_class, type) and issubclass(
                    processor_class, BaseProcessor
                ):
                    self._register_backend(processor_class)
                else:
                    logger.warning(
                        "Entry point %r in group %r did not provide a "
                        "BaseProcessor subclass (got %r); skipping it.",
                        name,
                        ENTRY_POINT_GROUP,
                        processor_class,
                    )
            self._discovered = True

    def register(self, processor_class: type[BaseProcessor]) -> None:
        """Manual registration for testing or dynamic use."""
        with self._lock:
            self._register_backend(processor_class)

    def unregister(self, slug: str) -> None:
        """Remove a backend by slug."""
        with self._lock:
            self._backends.pop(slug, None)

    def get_for_currency(self, currency: str) -> list[type[BaseProcessor]]:
        """Return all backends supporting the given currency."""
        self._ensure_discovered()
        return [
            backend
            for backend in self._backends.values()
            if currency in backend.accepted_currencies
        ]

    def get_choices(self, currency: str) -> list[tuple[str, str]]:
        """Return (slug, display_name) pairs for a currency."""
        return [
            (backend.slug, backend.display_name)
            for backend in self.get_for_currency(currency)
        ]

    def get_by_slug(self, slug: str) -> type[BaseProcessor]:
        """Return a backend class by slug.

        Raises ``BackendNotFoundError`` (a ``KeyError`` subclass, so
        legacy ``except KeyError`` callers keep working) when no backend
        is registered under ``slug``.
        """
        self._ensure_discovered()
        try:
            return self._backends[slug]
        except KeyError:
            raise BackendNotFoundError(
                f"No payment backend registered for slug {slug!r}.",
                context={"slug": slug},
            ) from None

    def get_all_currencies(self) -> set[str]:
        """Return all currencies supported by all backends."""
        self._ensure_discovered()
        currencies: set[str] = set()
        for backend in self._backends.values():
            currencies.update(backend.accepted_currencies)
        return currencies

    def _ensure_discovered(self) -> None:
        if not self._discovered:
            with self._lock:
                if not self._discovered:
                    self.discover()

    def _register_backend(self, processor_class: type[BaseProcessor]) -> None:
        slug = processor_class.slug
        if not slug:
            raise ValueError(
                "Cannot register backend "
                f"{processor_class.__module__}.{processor_class.__name__} "
                "with an empty slug."
            )
        existing = self._backends.get(slug)
        if existing is not None and existing is not processor_class:
            raise ValueError(
                f"Duplicate backend slug {slug!r}: "
                f"{existing.__module__}.{existing.__name__} and "
                f"{processor_class.__module__}.{processor_class.__name__}"
            )
        self._backends[slug] = processor_class


registry = PluginRegistry()
