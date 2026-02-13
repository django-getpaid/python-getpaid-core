# getpaid-core: Framework-Agnostic Payment Processing Core

**Date:** 2026-02-13
**Status:** Design
**Author:** AI-assisted design session

## Overview

`getpaid-core` is a framework-agnostic payment processing library that serves as
the foundation for `django-getpaid`, `litestar-getpaid`, and `fastapi-getpaid`.
It extracts the core payment logic from the existing `django-getpaid` library
into a pure Python package with no framework dependencies.

## Architecture

### Three-Layer Ecosystem

```
┌─────────────────────────────────────────────────────┐
│  Framework Adapters (thin)                          │
│  django-getpaid   litestar-getpaid   fastapi-getpaid│
│  - Views/routes                                     │
│  - Persistence (Repository implementation)          │
│  - Configuration loading                            │
│  - Template/form rendering                          │
└────────────────────┬────────────────────────────────┘
                     │ depends on
┌────────────────────▼────────────────────────────────┐
│  Payment Backends (framework-agnostic)              │
│  getpaid-payu   getpaid-paynow   getpaid-dotpay     │
│  - Subclass BaseProcessor (ABC)                     │
│  - Gateway HTTP communication via httpx             │
│  - Signature verification, response parsing         │
└────────────────────┬────────────────────────────────┘
                     │ depends on
┌────────────────────▼────────────────────────────────┐
│  getpaid-core                                       │
│  - Protocols: Order, Payment, PaymentRepository     │
│  - ABC: BaseProcessor (with shared helpers)         │
│  - FSM via 'transitions' library                    │
│  - Plugin registry (entry_points + manual)          │
│  - Types, enums, exceptions                         │
│  - Async-first (anyio), httpx for HTTP              │
└─────────────────────────────────────────────────────┘
```

### Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Contract definition | Hybrid: Protocols + ABCs | Protocols for framework integration points (structural subtyping); ABCs for processors (shared helper methods) |
| Async strategy | Async-first | Aligns with Litestar/FastAPI (async-native) and Django 5+ (growing async). Sync wrappers available. |
| State machine | `transitions` library | Framework-agnostic, well-tested, supports guards and callbacks. Avoids reinventing. |
| Plugin discovery | Entry points + manual fallback | Entry points are the primary mechanism (standard Python); manual `register()` for testing/dynamic use. |
| Persistence | Repository pattern | Core defines `PaymentRepository` protocol; framework adapters implement it for their ORM/storage. |
| HTTP client | httpx | Supports both sync and async, clean API. Backends use httpx directly. |
| Core thickness | Thick core, thin adapters | Core contains all business logic. Adapters are ~200-400 lines providing views, persistence, and config. |

## Package Structure

```
src/getpaid_core/
├── __init__.py          # Public API exports
├── enums.py             # PaymentStatus, FraudStatus, BackendMethod, ConfirmationMethod
├── types.py             # TypedDicts: TransactionResult, ChargeResponse, BuyerInfo, ItemInfo, etc.
├── exceptions.py        # GetPaidException hierarchy
├── protocols.py         # Protocols: Order, Payment, PaymentRepository
├── processor.py         # BaseProcessor ABC
├── registry.py          # PluginRegistry (entry_points + manual)
├── fsm.py               # Payment state machine (transitions library)
├── flow.py              # PaymentFlow: core payment orchestration
└── validators.py        # Pluggable payment validation system
```

## Core Abstractions

### Protocols (`protocols.py`)

Protocols define what framework adapters must provide. Any object with the right
shape satisfies the protocol -- no inheritance required.

```python
from decimal import Decimal
from typing import Protocol, runtime_checkable
from getpaid_core.types import BuyerInfo, ItemInfo

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
    status: str  # PaymentStatus value
    backend: str  # processor slug
    external_id: str
    description: str
    amount_paid: Decimal
    amount_locked: Decimal
    amount_refunded: Decimal
    fraud_status: str
    fraud_message: str


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
```

### Enums (`enums.py`)

Preserved from `django-getpaid` with identical values for backward compatibility:

```python
from enum import StrEnum

class PaymentStatus(StrEnum):
    NEW = 'new'
    PREPARED = 'prepared'
    PRE_AUTH = 'pre_auth'
    IN_CHARGE = 'in_charge'
    PARTIAL = 'partial'
    PAID = 'paid'
    FAILED = 'failed'
    REFUND_STARTED = 'refund_started'
    REFUNDED = 'refunded'

class FraudStatus(StrEnum):
    UNKNOWN = 'unknown'
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    CHECK = 'check'

class BackendMethod(StrEnum):
    GET = 'get'
    POST = 'post'
    REST = 'rest'

class ConfirmationMethod(StrEnum):
    PUSH = 'push'
    PULL = 'pull'
```

### Types (`types.py`)

```python
from decimal import Decimal
from typing import TypedDict

class BuyerInfo(TypedDict, total=False):
    email: str
    first_name: str
    last_name: str
    phone: str

class ItemInfo(TypedDict):
    name: str
    quantity: int
    unit_price: Decimal

class ChargeResponse(TypedDict):
    amount_charged: Decimal
    success: bool
    async_call: bool

class PaymentStatusResponse(TypedDict, total=False):
    amount: Decimal | None
    status: str | None
    external_id: str | None

class TransactionResult(TypedDict):
    """Returned by BaseProcessor.prepare_transaction().

    Framework adapters convert this into framework-specific HTTP responses:
    - GET: redirect to redirect_url
    - POST: render form that auto-submits to redirect_url with form_data
    - REST: return JSON or handle internally
    """
    redirect_url: str | None
    form_data: dict | None
    method: str  # 'GET', 'POST', or 'REST'
    headers: dict[str, str]
```

### Exceptions (`exceptions.py`)

```python
class GetPaidException(Exception):
    """Base exception for all getpaid errors."""

    def __init__(self, message: str = '', context: dict | None = None):
        super().__init__(message)
        self.context = context or {}

class CommunicationError(GetPaidException):
    """Error communicating with payment gateway."""

class ChargeFailure(CommunicationError):
    """Failed to charge payment."""

class LockFailure(CommunicationError):
    """Failed to lock (pre-authorize) payment."""

class RefundFailure(CommunicationError):
    """Failed to process refund."""

class CredentialsError(GetPaidException):
    """Invalid or missing gateway credentials."""

class InvalidCallbackError(GetPaidException):
    """Callback verification failed."""

class InvalidTransitionError(GetPaidException):
    """Attempted invalid state transition."""
```

## State Machine (`fsm.py`)

Uses the `transitions` library to define the payment lifecycle. The machine
attaches trigger methods directly to payment objects.

```python
from transitions import Machine
from getpaid_core.enums import PaymentStatus, FraudStatus

PAYMENT_TRANSITIONS = [
    # Preparation
    {
        'trigger': 'confirm_prepared',
        'source': PaymentStatus.NEW,
        'dest': PaymentStatus.PREPARED,
    },
    # Pre-authorization
    {
        'trigger': 'confirm_lock',
        'source': [PaymentStatus.NEW, PaymentStatus.PREPARED],
        'dest': PaymentStatus.PRE_AUTH,
    },
    # Charging
    {
        'trigger': 'confirm_charge_sent',
        'source': PaymentStatus.PRE_AUTH,
        'dest': PaymentStatus.IN_CHARGE,
    },
    # Payment received (partial or first payment)
    {
        'trigger': 'confirm_payment',
        'source': [
            PaymentStatus.PRE_AUTH,
            PaymentStatus.PREPARED,
            PaymentStatus.IN_CHARGE,
            PaymentStatus.PARTIAL,
        ],
        'dest': PaymentStatus.PARTIAL,
    },
    # Full payment confirmed
    {
        'trigger': 'mark_as_paid',
        'source': PaymentStatus.PARTIAL,
        'dest': PaymentStatus.PAID,
        'conditions': ['is_fully_paid'],
    },
    # Release pre-auth lock
    {
        'trigger': 'release_lock',
        'source': PaymentStatus.PRE_AUTH,
        'dest': PaymentStatus.REFUNDED,
    },
    # Refund flow
    {
        'trigger': 'start_refund',
        'source': [PaymentStatus.PAID, PaymentStatus.PARTIAL],
        'dest': PaymentStatus.REFUND_STARTED,
    },
    {
        'trigger': 'cancel_refund',
        'source': PaymentStatus.REFUND_STARTED,
        'dest': PaymentStatus.PARTIAL,
    },
    {
        'trigger': 'confirm_refund',
        'source': PaymentStatus.REFUND_STARTED,
        'dest': PaymentStatus.PARTIAL,
    },
    {
        'trigger': 'mark_as_refunded',
        'source': PaymentStatus.PARTIAL,
        'dest': PaymentStatus.REFUNDED,
        'conditions': ['is_fully_refunded'],
    },
    # Failure
    {
        'trigger': 'fail',
        'source': [
            PaymentStatus.NEW,
            PaymentStatus.PRE_AUTH,
            PaymentStatus.PREPARED,
        ],
        'dest': PaymentStatus.FAILED,
    },
]

FRAUD_TRANSITIONS = [
    {
        'trigger': 'flag_as_fraud',
        'source': FraudStatus.UNKNOWN,
        'dest': FraudStatus.REJECTED,
    },
    {
        'trigger': 'flag_as_legit',
        'source': FraudStatus.UNKNOWN,
        'dest': FraudStatus.ACCEPTED,
    },
    {
        'trigger': 'flag_for_check',
        'source': FraudStatus.UNKNOWN,
        'dest': FraudStatus.CHECK,
    },
    {
        'trigger': 'mark_as_fraud',
        'source': FraudStatus.CHECK,
        'dest': FraudStatus.REJECTED,
    },
    {
        'trigger': 'mark_as_legit',
        'source': FraudStatus.CHECK,
        'dest': FraudStatus.ACCEPTED,
    },
]

ALLOWED_CALLBACKS: frozenset[str] = frozenset({
    'confirm_prepared',
    'confirm_lock',
    'confirm_charge_sent',
    'confirm_payment',
    'mark_as_paid',
    'release_lock',
    'start_refund',
    'cancel_refund',
    'confirm_refund',
    'mark_as_refunded',
    'fail',
})

def create_payment_machine(payment) -> Machine:
    """Attach payment FSM to a payment object.

    The transitions library adds trigger methods (confirm_prepared,
    confirm_lock, etc.) directly to the payment object.
    """
    return Machine(
        model=payment,
        states=[s.value for s in PaymentStatus],
        transitions=PAYMENT_TRANSITIONS,
        initial=payment.status or PaymentStatus.NEW,
        model_attribute='status',
        auto_transitions=False,
    )

def create_fraud_machine(payment) -> Machine:
    """Attach fraud status FSM to a payment object."""
    return Machine(
        model=payment,
        states=[s.value for s in FraudStatus],
        transitions=FRAUD_TRANSITIONS,
        initial=payment.fraud_status or FraudStatus.UNKNOWN,
        model_attribute='fraud_status',
        auto_transitions=False,
    )
```

## BaseProcessor ABC (`processor.py`)

The abstract base class for payment backends. Retains shared helper methods
but has no framework dependencies.

```python
from abc import ABC, abstractmethod
from decimal import Decimal

from getpaid_core.protocols import Payment
from getpaid_core.types import (
    ChargeResponse,
    PaymentStatusResponse,
    TransactionResult,
)

class BaseProcessor(ABC):
    """Base class for payment backend processors.

    Subclasses must set class attributes and implement
    prepare_transaction(). Other methods are optional depending
    on the payment gateway's capabilities.
    """

    # --- Class attributes (set by subclass) ---
    slug: str = ''
    display_name: str = ''
    accepted_currencies: list[str] = []
    logo_url: str | None = None
    sandbox_url: str = ''
    production_url: str = ''

    def __init__(
        self, payment: Payment, config: dict | None = None
    ) -> None:
        self.payment = payment
        self.config = config or {}

    # --- Helpers ---

    def get_setting(self, name: str, default=None):
        """Read a setting from backend config."""
        return self.config.get(name, default)

    def get_paywall_baseurl(self) -> str:
        """Return the payment gateway base URL.

        Uses sandbox_url if config['sandbox'] is True (default),
        production_url otherwise.
        """
        sandbox = self.get_setting('sandbox', True)
        return self.sandbox_url if sandbox else self.production_url

    # --- Abstract method (MUST implement) ---

    @abstractmethod
    async def prepare_transaction(
        self, **kwargs
    ) -> TransactionResult:
        """Prepare data for initiating a payment.

        Returns a TransactionResult dict that the framework adapter
        converts into an HTTP response (redirect, form POST, or JSON).
        """
        ...

    # --- Optional methods ---

    async def verify_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        """Verify callback authenticity.

        Raise GetPaidException (or subclass) to reject the callback.
        Default: no verification (no-op).
        """

    async def handle_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        """Handle an async PUSH callback from the payment gateway.

        Parse the callback data and apply appropriate FSM transitions
        to self.payment.
        """
        raise NotImplementedError

    async def fetch_payment_status(
        self, **kwargs
    ) -> PaymentStatusResponse:
        """PULL flow: fetch current payment status from gateway."""
        raise NotImplementedError

    async def charge(
        self, amount: Decimal | None = None, **kwargs
    ) -> ChargeResponse:
        """Charge a pre-authorized payment."""
        raise NotImplementedError

    async def release_lock(self, **kwargs) -> Decimal:
        """Release a pre-authorized lock. Return the locked amount."""
        raise NotImplementedError

    async def start_refund(
        self, amount: Decimal | None = None, **kwargs
    ) -> Decimal:
        """Start a refund. Return the refund amount."""
        raise NotImplementedError

    async def cancel_refund(self, **kwargs) -> bool:
        """Cancel an in-progress refund. Return True if successful."""
        raise NotImplementedError
```

## Plugin Registry (`registry.py`)

Entry points are the primary discovery mechanism. Manual registration supports
testing and dynamic scenarios.

```python
from importlib.metadata import entry_points

from getpaid_core.processor import BaseProcessor

ENTRY_POINT_GROUP = 'getpaid.backends'

class PluginRegistry:
    def __init__(self) -> None:
        self._backends: dict[str, type[BaseProcessor]] = {}
        self._discovered = False

    def discover(self) -> None:
        """Load all backends registered via entry_points."""
        eps = entry_points(group=ENTRY_POINT_GROUP)
        for ep in eps:
            processor_class = ep.load()
            if isinstance(processor_class, type) and issubclass(
                processor_class, BaseProcessor
            ):
                self._backends[processor_class.slug] = processor_class
        self._discovered = True

    def register(self, processor_class: type[BaseProcessor]) -> None:
        """Manual registration for testing or dynamic use."""
        self._backends[processor_class.slug] = processor_class

    def unregister(self, slug: str) -> None:
        """Remove a backend by slug."""
        self._backends.pop(slug, None)

    def get_for_currency(
        self, currency: str
    ) -> list[type[BaseProcessor]]:
        """Return all backends that support the given currency."""
        self._ensure_discovered()
        return [
            b
            for b in self._backends.values()
            if currency in b.accepted_currencies
        ]

    def get_choices(
        self, currency: str
    ) -> list[tuple[str, str]]:
        """Return (slug, display_name) pairs for a currency."""
        return [
            (b.slug, b.display_name)
            for b in self.get_for_currency(currency)
        ]

    def get_by_slug(self, slug: str) -> type[BaseProcessor]:
        """Return a backend class by its slug."""
        self._ensure_discovered()
        return self._backends[slug]

    def get_all_currencies(self) -> set[str]:
        """Return all currencies supported across all backends."""
        self._ensure_discovered()
        currencies: set[str] = set()
        for b in self._backends.values():
            currencies.update(b.accepted_currencies)
        return currencies

    def _ensure_discovered(self) -> None:
        if not self._discovered:
            self.discover()


# Module-level singleton
registry = PluginRegistry()
```

### Backend registration via `pyproject.toml`

```toml
[project.entry-points."getpaid.backends"]
payu = "getpaid_payu.processor:PaymentProcessor"
```

## Payment Flow Orchestrator (`flow.py`)

The main entry point for framework adapters. Orchestrates the interaction
between repository, processor, and state machine.

```python
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.fsm import (
    ALLOWED_CALLBACKS,
    create_fraud_machine,
    create_payment_machine,
)
from getpaid_core.protocols import Order, Payment, PaymentRepository
from getpaid_core.registry import registry
from getpaid_core.types import TransactionResult
from getpaid_core.validators import run_validators


class PaymentFlow:
    """Core payment processing orchestrator.

    Framework adapters create an instance with their repository
    implementation and backend configuration, then delegate to
    its methods from views/routes.
    """

    def __init__(
        self,
        repository: PaymentRepository,
        config: dict | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or {}

    async def create_payment(
        self, order: Order, backend_slug: str, **kwargs
    ) -> Payment:
        """Create a new payment for an order."""
        registry.get_by_slug(backend_slug)  # validate slug exists
        payment = await self.repository.create(
            order=order,
            backend=backend_slug,
            amount_required=order.get_total_amount(),
            currency=order.get_currency(),
            description=order.get_description(),
            **kwargs,
        )
        return payment

    async def prepare(
        self, payment: Payment, **kwargs
    ) -> TransactionResult:
        """Prepare a payment for processing.

        Runs validators, calls the backend's prepare_transaction(),
        transitions payment to PREPARED, and persists.
        Returns a TransactionResult for the adapter to convert
        into a framework-specific HTTP response.
        """
        run_validators(payment)
        create_payment_machine(payment)
        processor = self._get_processor(payment)
        result = await processor.prepare_transaction(**kwargs)
        payment.confirm_prepared()
        await self.repository.save(payment)
        return result

    async def handle_callback(
        self,
        payment: Payment,
        data: dict,
        headers: dict,
        **kwargs,
    ) -> None:
        """Handle an incoming PUSH callback from the gateway.

        Verifies the callback, then delegates to the backend
        processor for status parsing and FSM transitions.
        """
        processor = self._get_processor(payment)
        await processor.verify_callback(data, headers, **kwargs)
        create_payment_machine(payment)
        create_fraud_machine(payment)
        await processor.handle_callback(data, headers, **kwargs)
        await self.repository.save(payment)

    async def fetch_and_update_status(
        self, payment: Payment
    ) -> Payment:
        """PULL flow: fetch status from gateway and update."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        create_fraud_machine(payment)
        response = await processor.fetch_payment_status()
        if response.get('status'):
            callback = response['status']
            if callback in ALLOWED_CALLBACKS:
                trigger = getattr(payment, callback, None)
                if trigger and callable(trigger):
                    trigger()
            else:
                raise InvalidTransitionError(
                    f'Callback {callback!r} not in ALLOWED_CALLBACKS'
                )
        await self.repository.save(payment)
        return payment

    async def charge(
        self, payment: Payment, amount=None, **kwargs
    ):
        """Charge a pre-authorized payment."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        result = await processor.charge(amount=amount, **kwargs)
        if result['success']:
            payment.confirm_charge_sent()
        await self.repository.save(payment)
        return result

    async def release_lock(self, payment: Payment, **kwargs):
        """Release a pre-authorized lock."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        amount = await processor.release_lock(**kwargs)
        payment.release_lock()
        await self.repository.save(payment)
        return amount

    async def start_refund(
        self, payment: Payment, amount=None, **kwargs
    ):
        """Start a refund."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        refund_amount = await processor.start_refund(
            amount=amount, **kwargs
        )
        payment.start_refund()
        await self.repository.save(payment)
        return refund_amount

    async def cancel_refund(self, payment: Payment, **kwargs):
        """Cancel an in-progress refund."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        success = await processor.cancel_refund(**kwargs)
        if success:
            payment.cancel_refund()
            await self.repository.save(payment)
        return success

    def _get_processor(self, payment: Payment):
        """Instantiate the processor for a payment."""
        processor_class = registry.get_by_slug(payment.backend)
        backend_config = self.config.get(payment.backend, {})
        return processor_class(payment, config=backend_config)
```

## Framework Adapter Responsibilities

Each adapter is a thin wrapper (~200-400 lines):

| Responsibility | Django | Litestar | FastAPI |
|----------------|--------|----------|---------|
| **Persistence** | `DjangoPaymentRepository` (ORM) | `LitestarPaymentRepository` (Advanced Alchemy) | `FastAPIPaymentRepository` (SQLAlchemy) |
| **Payment Model** | Django model (swappable) | SQLAlchemy model | SQLAlchemy model |
| **Order Protocol** | Abstract Django model | Protocol mixin | Protocol mixin |
| **Create Payment** | `CreatePaymentView` (CBV) | Route handler | Route handler |
| **Callback** | `CallbackView` (CBV) | Route handler | Route handler |
| **Redirects** | `SuccessView`, `FailureView` | Route handlers | Route handlers |
| **URL Registration** | `urls.py` | `Router` | `APIRouter` |
| **Configuration** | `settings.GETPAID_BACKENDS` | `app.state` / plugin config | `app.state` / `Settings` |
| **Templates** | Django templates + forms | Jinja2 | API-only or Jinja2 |

### Example: Django adapter view

```python
# django-getpaid v3 view (simplified)
from getpaid_core.flow import PaymentFlow

class CreatePaymentView(View):
    async def post(self, request):
        flow = PaymentFlow(
            repository=DjangoPaymentRepository(),
            config=get_backend_config(),
        )
        payment = await flow.create_payment(order, backend_slug)
        result = await flow.prepare(payment)
        return self._build_response(request, result)

    def _build_response(self, request, result):
        if result['method'] == 'GET' and result['redirect_url']:
            return HttpResponseRedirect(result['redirect_url'])
        elif result['method'] == 'POST':
            return render(
                request,
                'getpaid/payment_post_form.html',
                {
                    'form_data': result['form_data'],
                    'action_url': result['redirect_url'],
                },
            )
        else:
            return JsonResponse(result)
```

### Example: Litestar adapter route

```python
# litestar-getpaid route (simplified)
from getpaid_core.flow import PaymentFlow

@post('/payments/create')
async def create_payment(
    request: Request,
    data: CreatePaymentDTO,
    repository: PaymentRepository,
) -> Response:
    flow = PaymentFlow(
        repository=repository,
        config=request.app.state.getpaid_config,
    )
    payment = await flow.create_payment(
        order, data.backend_slug
    )
    result = await flow.prepare(payment)
    if result['method'] == 'GET':
        return Redirect(result['redirect_url'])
    return Response(content=result)
```

## Testing Strategy

### Level 1: Core Unit Tests (`getpaid-core/tests/`)

Test core logic in isolation with mock objects:

```
tests/
├── conftest.py              # Mock payment, order, repository fixtures
├── test_enums.py            # Enum values and membership
├── test_fsm.py              # Every valid/invalid state transition
├── test_registry.py         # Discovery, registration, currency filtering
├── test_flow.py             # PaymentFlow with mock repo + processor
├── test_validators.py       # Validator system
└── test_exceptions.py       # Exception hierarchy
```

### Level 2: Backend Integration Tests (per `getpaid-*` plugin)

Test each backend against mocked HTTP responses using `respx`:

```
# getpaid-payu/tests/
├── conftest.py              # PayU-specific fixtures, recorded responses
├── test_prepare.py          # Transaction preparation
├── test_callback.py         # Callback parsing + signature verification
├── test_status.py           # Status fetching (PULL flow)
├── test_refund.py           # Refund operations
└── test_client.py           # HTTP client against mocked endpoints
```

### Level 3: Framework Adapter Tests (per framework)

Full end-to-end tests with real persistence:

```
# django-getpaid/tests/
├── test_views.py            # Full request/response cycle
├── test_repository.py       # DjangoPaymentRepository against real DB
├── test_integration.py      # Create -> prepare -> callback -> paid

# litestar-getpaid/tests/
├── test_routes.py           # Full request/response with TestClient
├── test_repository.py       # LitestarPaymentRepository against DB
├── test_integration.py      # Full flow

# fastapi-getpaid/tests/
├── test_routes.py           # Full request/response with httpx TestClient
├── test_repository.py       # FastAPIPaymentRepository against DB
├── test_integration.py      # Full flow
```

All three framework test suites validate the same scenarios:

1. Create payment, get redirect URL
2. Receive PUSH callback, verify status transition
3. PULL flow: fetch and update status
4. Pre-auth -> charge flow
5. Refund flow (start, confirm, cancel)
6. Error handling (invalid callback, unknown backend, bad signature)

## Plugin Conversion Path

### Renaming Convention

| Old (Django-coupled) | New (framework-agnostic) |
|---------------------|--------------------------|
| `django-getpaid-payu` | `getpaid-payu` |
| `django-getpaid-paynow` | `getpaid-paynow` |
| `django-getpaid-dotpay` | `getpaid-dotpay` |
| `django-getpaid-imoje` | `getpaid-imoje` |
| `django-getpaid-bitpay` | `getpaid-bitpay` |

### Conversion Steps (per plugin)

1. **Rename** package: `django-getpaid-X` -> `getpaid-X`
2. **Remove Django imports**: No `django.http`, `django.views`, `django.conf`
3. **Make methods async**: All processor methods become `async def`
4. **Return data, not responses**: `prepare_transaction()` returns `TransactionResult`
5. **Accept raw data**: `handle_callback(data, headers)` instead of `handle_callback(request)`
6. **Config via constructor**: Config dict in `__init__`, not from `django.conf.settings`
7. **Use httpx**: Replace `requests` with `httpx.AsyncClient`
8. **Register via entry_points**: Add `[project.entry-points."getpaid.backends"]`
9. **Move views**: Django views move to `django-getpaid` adapter or remain as optional extras

### Plugin Priority

| Plugin | State | Action | Effort |
|--------|-------|--------|--------|
| `getpaid-payu` | ~85% | Convert, remove Django deps | Medium |
| `getpaid-paynow` | ~50% | Convert, fix tests | Medium |
| `getpaid-dotpay` | ~60% | Full modernization | High |
| `getpaid-imoje` | ~15% | **Rewrite from scratch** | High |
| `getpaid-bitpay` | ~10% | Skeleton, implement API | High |

## Cookiecutter Update

`cookiecutter-getpaid-backend` will be rewritten to generate framework-agnostic
`getpaid-*` plugins:

- Generates `processor.py` with correct async `BaseProcessor` interface
- Generates `pyproject.toml` with entry_points registration and modern tooling
- Generates test scaffold using `respx` for httpx mocking
- Generates README with step-by-step howto guide
- No Django dependency in the generated plugin
- Uses `uv` for dependency management
- GitHub Actions CI (not Travis)

## Backward Compatibility

`django-getpaid` v3.0 becomes a thin adapter over `getpaid-core`:

- Still provides Django models, views, URLs, forms, template tags
- Internally delegates to `PaymentFlow` from `getpaid-core`
- **Breaking change**: backends must be framework-agnostic `getpaid-*` packages
- Migration guide documents all changes from v2.x to v3.0

## Dependencies

### `getpaid-core` runtime dependencies

| Package | Purpose |
|---------|---------|
| `transitions` | State machine library |
| `httpx` | Async HTTP client |
| `anyio` | Async runtime portability |

### Per-framework adapter dependencies

| Adapter | Additional Dependencies |
|---------|------------------------|
| `django-getpaid` | `Django>=5.2`, `swapper`, `getpaid-core` |
| `litestar-getpaid` | `litestar`, `advanced-alchemy`, `getpaid-core` |
| `fastapi-getpaid` | `fastapi`, `sqlalchemy`, `getpaid-core` |
