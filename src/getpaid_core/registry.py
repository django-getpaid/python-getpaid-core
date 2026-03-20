"""Plugin registry for payment backends."""

from importlib.metadata import entry_points

from getpaid_core.processor import BaseProcessor


ENTRY_POINT_GROUP = "getpaid.backends"


class PluginRegistry:
    """Discovers and stores payment backend processors."""

    def __init__(self) -> None:
        self._backends: dict[str, type[BaseProcessor]] = {}
        self._discovered = False

    def discover(self) -> None:
        """Load all backends registered via entry points."""
        for entry_point in entry_points(group=ENTRY_POINT_GROUP):
            processor_class = entry_point.load()
            if isinstance(processor_class, type) and issubclass(
                processor_class, BaseProcessor
            ):
                self._register_backend(processor_class)
        self._discovered = True

    def register(self, processor_class: type[BaseProcessor]) -> None:
        """Manual registration for testing or dynamic use."""
        self._register_backend(processor_class)

    def unregister(self, slug: str) -> None:
        """Remove a backend by slug."""
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
        """Return a backend class by slug. Raises KeyError."""
        self._ensure_discovered()
        return self._backends[slug]

    def get_all_currencies(self) -> set[str]:
        """Return all currencies supported by all backends."""
        self._ensure_discovered()
        currencies: set[str] = set()
        for backend in self._backends.values():
            currencies.update(backend.accepted_currencies)
        return currencies

    def _ensure_discovered(self) -> None:
        if not self._discovered:
            self.discover()

    def _register_backend(self, processor_class: type[BaseProcessor]) -> None:
        slug = processor_class.slug
        existing = self._backends.get(slug)
        if existing is not None and existing is not processor_class:
            raise ValueError(
                f"Duplicate backend slug {slug!r}: "
                f"{existing.__module__}.{existing.__name__} and "
                f"{processor_class.__module__}.{processor_class.__name__}"
            )
        self._backends[slug] = processor_class


registry = PluginRegistry()
