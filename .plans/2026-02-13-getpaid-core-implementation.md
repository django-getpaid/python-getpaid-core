# getpaid-core Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement getpaid-core, a framework-agnostic payment processing library that extracts core logic from django-getpaid into a pure Python package with no framework dependencies.

**Architecture:** Three-layer ecosystem: `getpaid-core` (protocols, FSM, processor ABC, registry, flow orchestrator) -> payment backends (framework-agnostic plugins) -> framework adapters (thin wrappers for Django/Litestar/FastAPI). Async-first using anyio, httpx for HTTP, `transitions` library for FSM, entry_points for plugin discovery.

**Tech Stack:** Python 3.10+, transitions (FSM), httpx (HTTP), anyio (async), pytest + pytest-asyncio (testing), uv (dependency management), ruff (linting)

**Design document:** `docs/plans/2026-02-13-getpaid-core-design.md`

**Reference implementation:** `/home/minder/projekty/django-getpaid/django-getpaid/getpaid/` -- the existing django-getpaid library whose core logic we are extracting.

**Working directory:** `/home/minder/projekty/django-getpaid/getpaid-core`

---

## Phase 0: Project Setup

### Task 1: Modernize pyproject.toml

The current `pyproject.toml` uses the old Poetry format with outdated dependencies (black, flake8, etc. from 2022). Replace it with a modern `[project]` format using uv, ruff, and current dependencies.

**Files:**
- Modify: `pyproject.toml`
- Delete: `noxfile.py` (replace nox with simple pytest/tox)
- Delete: `setup.cfg` (move config to pyproject.toml)
- Delete: `.pre-commit-config.yaml` (rewrite for ruff)
- Delete: `src/getpaid_core/__main__.py` (no CLI needed for core library)

**Step 1: Rewrite pyproject.toml**

Replace the entire file with:

```toml
[project]
name = 'getpaid-core'
version = '0.1.0'
description = 'Framework-agnostic payment processing core.'
readme = 'README.md'
license = {text = 'MIT'}
authors = [
    {name = 'Dominik Kozaczko', email = 'dominik@kozaczko.info'},
]
requires-python = '>=3.10'
classifiers = [
    'Development Status :: 3 - Alpha',
    'Intended Audience :: Developers',
    'License :: OSI Approved :: MIT License',
    'Programming Language :: Python :: 3.10',
    'Programming Language :: Python :: 3.11',
    'Programming Language :: Python :: 3.12',
    'Programming Language :: Python :: 3.13',
    'Topic :: Office/Business :: Financial',
    'Topic :: Office/Business :: Financial :: Point-Of-Sale',
    'Typing :: Typed',
]
dependencies = [
    'transitions>=0.9.0',
    'httpx>=0.27.0',
    'anyio>=4.0',
]

[dependency-groups]
dev = [
    'pytest>=8.0',
    'pytest-asyncio>=0.24.0',
    'pytest-cov>=5.0',
    'respx>=0.22.0',
    'ruff>=0.9.0',
    'pre-commit>=4.0',
]

[project.urls]
Homepage = 'https://github.com/dekoza/getpaid-core'
Repository = 'https://github.com/dekoza/getpaid-core'
Documentation = 'https://getpaid-core.readthedocs.io'
Changelog = 'https://github.com/dekoza/getpaid-core/releases'

[build-system]
requires = ['hatchling']
build-backend = 'hatchling.build'

[tool.hatch.build.targets.wheel]
packages = ['src/getpaid_core']

[tool.pytest.ini_options]
testpaths = ['tests']
asyncio_mode = 'auto'

[tool.coverage.run]
branch = true
source = ['getpaid_core']

[tool.coverage.report]
show_missing = true

[tool.ruff]
target-version = 'py310'
line-length = 80
src = ['src', 'tests']

[tool.ruff.lint]
select = [
    'E',    # pycodestyle errors
    'W',    # pycodestyle warnings
    'F',    # pyflakes
    'I',    # isort
    'N',    # pep8-naming
    'UP',   # pyupgrade
    'B',    # flake8-bugbear
    'A',    # flake8-builtins
    'SIM',  # flake8-simplify
    'TCH',  # type-checking imports
    'RUF',  # ruff-specific
]

[tool.ruff.lint.isort]
force-single-line = true
lines-after-imports = 2
known-first-party = ['getpaid_core']
```

**Step 2: Delete obsolete files**

```bash
rm -f noxfile.py setup.cfg src/getpaid_core/__main__.py .pre-commit-config.yaml pytest.ini
```

**Step 3: Create .pre-commit-config.yaml (minimal, ruff-based)**

```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.9.6
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v5.0.0
    hooks:
      - id: check-toml
      - id: check-yaml
      - id: end-of-file-fixer
      - id: trailing-whitespace
```

**Step 4: Update __init__.py**

Replace `src/getpaid_core/__init__.py` with:

```python
"""Getpaid Core -- framework-agnostic payment processing."""

__version__ = '0.1.0'
```

**Step 5: Install dependencies and verify**

```bash
uv sync
uv run pytest --co -q
```

Expected: pytest collects 0 tests (existing stubs have no assertions).

**Step 6: Commit**

```bash
git add -A
git commit -m "chore: modernize project setup (uv, ruff, hatchling)"
```

---

## Phase 1: Core Types (no dependencies between modules)

### Task 2: Enums

**Files:**
- Create: `src/getpaid_core/enums.py`
- Create: `tests/test_enums.py`

**Step 1: Write the failing test**

Write `tests/test_enums.py`:

```python
"""Tests for getpaid_core.enums."""

from getpaid_core.enums import BackendMethod
from getpaid_core.enums import ConfirmationMethod
from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus


class TestPaymentStatus:
    """Payment status enum values match django-getpaid for compat."""

    def test_new(self):
        assert PaymentStatus.NEW == 'new'

    def test_prepared(self):
        assert PaymentStatus.PREPARED == 'prepared'

    def test_pre_auth(self):
        assert PaymentStatus.PRE_AUTH == 'pre-auth'

    def test_in_charge(self):
        assert PaymentStatus.IN_CHARGE == 'charge_started'

    def test_partial(self):
        assert PaymentStatus.PARTIAL == 'partially_paid'

    def test_paid(self):
        assert PaymentStatus.PAID == 'paid'

    def test_failed(self):
        assert PaymentStatus.FAILED == 'failed'

    def test_refund_started(self):
        assert PaymentStatus.REFUND_STARTED == 'refund_started'

    def test_refunded(self):
        assert PaymentStatus.REFUNDED == 'refunded'

    def test_member_count(self):
        assert len(PaymentStatus) == 9

    def test_is_str_subclass(self):
        assert isinstance(PaymentStatus.NEW, str)


class TestFraudStatus:
    """Fraud status enum values match django-getpaid for compat."""

    def test_unknown(self):
        assert FraudStatus.UNKNOWN == 'unknown'

    def test_accepted(self):
        assert FraudStatus.ACCEPTED == 'accepted'

    def test_rejected(self):
        assert FraudStatus.REJECTED == 'rejected'

    def test_check(self):
        assert FraudStatus.CHECK == 'check'

    def test_member_count(self):
        assert len(FraudStatus) == 4


class TestBackendMethod:
    def test_get(self):
        assert BackendMethod.GET == 'GET'

    def test_post(self):
        assert BackendMethod.POST == 'POST'

    def test_rest(self):
        assert BackendMethod.REST == 'REST'

    def test_member_count(self):
        assert len(BackendMethod) == 3


class TestConfirmationMethod:
    def test_push(self):
        assert ConfirmationMethod.PUSH == 'PUSH'

    def test_pull(self):
        assert ConfirmationMethod.PULL == 'PULL'

    def test_member_count(self):
        assert len(ConfirmationMethod) == 2
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_enums.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'getpaid_core.enums'`

**Step 3: Write the implementation**

Create `src/getpaid_core/enums.py`:

```python
"""Payment processing enums.

Values are kept identical to django-getpaid for backward compatibility.
"""

from enum import StrEnum


class PaymentStatus(StrEnum):
    """Internal payment status."""

    NEW = 'new'
    PREPARED = 'prepared'
    PRE_AUTH = 'pre-auth'
    IN_CHARGE = 'charge_started'
    PARTIAL = 'partially_paid'
    PAID = 'paid'
    FAILED = 'failed'
    REFUND_STARTED = 'refund_started'
    REFUNDED = 'refunded'


class FraudStatus(StrEnum):
    """Fraud verification status."""

    UNKNOWN = 'unknown'
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    CHECK = 'check'


class BackendMethod(StrEnum):
    """HTTP method used to initiate payment."""

    GET = 'GET'
    POST = 'POST'
    REST = 'REST'


class ConfirmationMethod(StrEnum):
    """How the payment gateway confirms payment status."""

    PUSH = 'PUSH'
    PULL = 'PULL'
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_enums.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/getpaid_core/enums.py tests/test_enums.py
git commit -m "feat: add payment processing enums"
```

---

### Task 3: Types (TypedDicts)

**Files:**
- Create: `src/getpaid_core/types.py`
- Create: `tests/test_types.py`

**Step 1: Write the failing test**

Write `tests/test_types.py`:

```python
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
            'email': 'test@example.com',
            'first_name': 'Jan',
            'last_name': 'Kowalski',
            'phone': '+48123456789',
        }
        assert info['email'] == 'test@example.com'

    def test_create_partial(self):
        """BuyerInfo has total=False, all fields optional."""
        info: BuyerInfo = {'email': 'test@example.com'}
        assert info['email'] == 'test@example.com'

    def test_empty_is_valid(self):
        info: BuyerInfo = {}
        assert isinstance(info, dict)


class TestItemInfo:
    def test_create(self):
        item: ItemInfo = {
            'name': 'Widget',
            'quantity': 2,
            'unit_price': Decimal('9.99'),
        }
        assert item['name'] == 'Widget'
        assert item['quantity'] == 2
        assert item['unit_price'] == Decimal('9.99')


class TestChargeResponse:
    def test_create(self):
        resp: ChargeResponse = {
            'amount_charged': Decimal('100.00'),
            'success': True,
            'async_call': False,
        }
        assert resp['success'] is True


class TestPaymentStatusResponse:
    def test_create_full(self):
        resp: PaymentStatusResponse = {
            'amount': Decimal('50.00'),
            'status': 'paid',
            'external_id': 'ext-123',
        }
        assert resp['status'] == 'paid'

    def test_create_empty(self):
        """PaymentStatusResponse has total=False."""
        resp: PaymentStatusResponse = {}
        assert isinstance(resp, dict)


class TestTransactionResult:
    def test_redirect(self):
        result: TransactionResult = {
            'redirect_url': 'https://pay.example.com/123',
            'form_data': None,
            'method': 'GET',
            'headers': {},
        }
        assert result['method'] == 'GET'
        assert result['redirect_url'] == 'https://pay.example.com/123'

    def test_post_form(self):
        result: TransactionResult = {
            'redirect_url': 'https://pay.example.com/form',
            'form_data': {'token': 'abc', 'amount': '100'},
            'method': 'POST',
            'headers': {'X-Signature': 'sig123'},
        }
        assert result['method'] == 'POST'
        assert result['form_data']['token'] == 'abc'

    def test_rest(self):
        result: TransactionResult = {
            'redirect_url': None,
            'form_data': None,
            'method': 'REST',
            'headers': {},
        }
        assert result['redirect_url'] is None
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_types.py -v
```

Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `src/getpaid_core/types.py`:

```python
"""Core type definitions for payment processing."""

from decimal import Decimal
from typing import TypedDict


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


class ChargeResponse(TypedDict):
    """Response from charging a pre-authorized payment."""

    amount_charged: Decimal
    success: bool
    async_call: bool


class PaymentStatusResponse(TypedDict, total=False):
    """Response from fetching payment status from gateway."""

    amount: Decimal | None
    status: str | None
    external_id: str | None


class TransactionResult(TypedDict):
    """Result of preparing a transaction.

    Framework adapters convert this into framework-specific responses:
    - GET: redirect to redirect_url
    - POST: render form that auto-submits to redirect_url with form_data
    - REST: return JSON or handle internally
    """

    redirect_url: str | None
    form_data: dict | None
    method: str  # 'GET', 'POST', or 'REST'
    headers: dict[str, str]
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_types.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/getpaid_core/types.py tests/test_types.py
git commit -m "feat: add core TypedDict types"
```

---

### Task 4: Exceptions

**Files:**
- Create: `src/getpaid_core/exceptions.py`
- Create: `tests/test_exceptions.py`

**Step 1: Write the failing test**

Write `tests/test_exceptions.py`:

```python
"""Tests for getpaid_core.exceptions."""

import pytest

from getpaid_core.exceptions import ChargeFailure
from getpaid_core.exceptions import CommunicationError
from getpaid_core.exceptions import CredentialsError
from getpaid_core.exceptions import GetPaidException
from getpaid_core.exceptions import InvalidCallbackError
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import LockFailure
from getpaid_core.exceptions import RefundFailure


class TestGetPaidException:
    def test_message(self):
        exc = GetPaidException('something went wrong')
        assert str(exc) == 'something went wrong'

    def test_context_default(self):
        exc = GetPaidException('error')
        assert exc.context == {}

    def test_context_provided(self):
        ctx = {'order_id': '123'}
        exc = GetPaidException('error', context=ctx)
        assert exc.context == ctx
        assert exc.context['order_id'] == '123'

    def test_is_exception(self):
        with pytest.raises(GetPaidException):
            raise GetPaidException('test')


class TestExceptionHierarchy:
    """Verify the inheritance tree matches django-getpaid."""

    def test_communication_error_is_getpaid(self):
        assert issubclass(CommunicationError, GetPaidException)

    def test_charge_failure_is_communication(self):
        assert issubclass(ChargeFailure, CommunicationError)

    def test_lock_failure_is_communication(self):
        assert issubclass(LockFailure, CommunicationError)

    def test_refund_failure_is_communication(self):
        assert issubclass(RefundFailure, CommunicationError)

    def test_credentials_error_is_getpaid(self):
        assert issubclass(CredentialsError, GetPaidException)

    def test_invalid_callback_is_getpaid(self):
        assert issubclass(InvalidCallbackError, GetPaidException)

    def test_invalid_transition_is_getpaid(self):
        assert issubclass(InvalidTransitionError, GetPaidException)


class TestExceptionContext:
    """All exceptions support the context kwarg."""

    def test_communication_error_context(self):
        exc = CommunicationError('fail', context={'url': '/pay'})
        assert exc.context['url'] == '/pay'

    def test_charge_failure_context(self):
        exc = ChargeFailure('fail', context={'amount': 100})
        assert exc.context['amount'] == 100
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_exceptions.py -v
```

Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `src/getpaid_core/exceptions.py`:

```python
"""Exception hierarchy for payment processing."""


class GetPaidException(Exception):
    """Base exception for all getpaid errors."""

    def __init__(
        self, message: str = '', context: dict | None = None
    ) -> None:
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

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_exceptions.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/getpaid_core/exceptions.py tests/test_exceptions.py
git commit -m "feat: add exception hierarchy"
```

---

## Phase 2: Protocols & Processor

### Task 5: Protocols

**Files:**
- Create: `src/getpaid_core/protocols.py`
- Create: `tests/test_protocols.py`

**Step 1: Write the failing test**

Write `tests/test_protocols.py`:

```python
"""Tests for getpaid_core.protocols."""

from decimal import Decimal

import pytest

from getpaid_core.protocols import Order
from getpaid_core.protocols import Payment
from getpaid_core.protocols import PaymentRepository


class ConcreteOrder:
    """A concrete class satisfying the Order protocol."""

    def get_total_amount(self) -> Decimal:
        return Decimal('100.00')

    def get_buyer_info(self):
        return {'email': 'test@example.com'}

    def get_description(self) -> str:
        return 'Test order'

    def get_currency(self) -> str:
        return 'PLN'

    def get_items(self):
        return []

    def get_return_url(self, success=None) -> str:
        return '/return/'


class IncompleteOrder:
    """Missing required methods."""

    def get_total_amount(self) -> Decimal:
        return Decimal('50.00')


class TestOrderProtocol:
    def test_concrete_order_satisfies_protocol(self):
        order = ConcreteOrder()
        assert isinstance(order, Order)

    def test_incomplete_order_does_not_satisfy(self):
        order = IncompleteOrder()
        assert not isinstance(order, Order)

    def test_protocol_is_runtime_checkable(self):
        """Order is decorated with @runtime_checkable."""
        assert isinstance(ConcreteOrder(), Order)


class ConcretePayment:
    """A concrete class satisfying the Payment protocol."""

    def __init__(self):
        self.id = 'pay-123'
        self.order = ConcreteOrder()
        self.amount_required = Decimal('100.00')
        self.currency = 'PLN'
        self.status = 'new'
        self.backend = 'dummy'
        self.external_id = ''
        self.description = 'Test'
        self.amount_paid = Decimal('0')
        self.amount_locked = Decimal('0')
        self.amount_refunded = Decimal('0')
        self.fraud_status = 'unknown'
        self.fraud_message = ''


class TestPaymentProtocol:
    def test_concrete_payment_satisfies_protocol(self):
        payment = ConcretePayment()
        assert isinstance(payment, Payment)

    def test_protocol_is_runtime_checkable(self):
        assert isinstance(ConcretePayment(), Payment)


class TestPaymentRepositoryProtocol:
    def test_protocol_is_runtime_checkable(self):
        """PaymentRepository is @runtime_checkable."""
        # Just verify the protocol class exists and is importable
        assert PaymentRepository is not None
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_protocols.py -v
```

Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `src/getpaid_core/protocols.py`:

```python
"""Protocols defining framework integration contracts.

Framework adapters (django-getpaid, litestar-getpaid, etc.) provide
concrete implementations. Any object with the right shape satisfies
the protocol -- no inheritance required.
"""

from decimal import Decimal
from typing import Protocol
from typing import runtime_checkable

from getpaid_core.types import BuyerInfo
from getpaid_core.types import ItemInfo


@runtime_checkable
class Order(Protocol):
    """What the core expects from an order object."""

    def get_total_amount(self) -> Decimal: ...
    def get_buyer_info(self) -> BuyerInfo: ...
    def get_description(self) -> str: ...
    def get_currency(self) -> str: ...
    def get_items(self) -> list[ItemInfo]: ...
    def get_return_url(
        self, success: bool | None = None
    ) -> str: ...


@runtime_checkable
class Payment(Protocol):
    """What the core expects from a payment object."""

    id: str
    order: Order
    amount_required: Decimal
    currency: str
    status: str
    backend: str
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
    async def list_by_order(
        self, order_id: str
    ) -> list[Payment]: ...
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_protocols.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/getpaid_core/protocols.py tests/test_protocols.py
git commit -m "feat: add Order, Payment, PaymentRepository protocols"
```

---

### Task 6: BaseProcessor ABC

**Files:**
- Create: `src/getpaid_core/processor.py`
- Create: `tests/test_processor.py`

The BaseProcessor is the abstract base class that all payment backends inherit from. It provides shared helpers (`get_setting`, `get_paywall_baseurl`) and defines the abstract interface.

**Step 1: Write the failing test**

Write `tests/test_processor.py`:

```python
"""Tests for getpaid_core.processor.BaseProcessor."""

from decimal import Decimal

import pytest

from getpaid_core.processor import BaseProcessor
from getpaid_core.types import TransactionResult


# -- Test fixtures --

class ConcreteOrder:
    def get_total_amount(self):
        return Decimal('100.00')
    def get_buyer_info(self):
        return {'email': 'test@example.com'}
    def get_description(self):
        return 'Test'
    def get_currency(self):
        return 'PLN'
    def get_items(self):
        return []
    def get_return_url(self, success=None):
        return '/return/'


class ConcretePayment:
    def __init__(self):
        self.id = 'pay-1'
        self.order = ConcreteOrder()
        self.amount_required = Decimal('100.00')
        self.currency = 'PLN'
        self.status = 'new'
        self.backend = 'test'
        self.external_id = ''
        self.description = 'Test'
        self.amount_paid = Decimal('0')
        self.amount_locked = Decimal('0')
        self.amount_refunded = Decimal('0')
        self.fraud_status = 'unknown'
        self.fraud_message = ''


class DummyProcessor(BaseProcessor):
    slug = 'dummy'
    display_name = 'Dummy'
    accepted_currencies = ['PLN', 'EUR']
    sandbox_url = 'https://sandbox.example.com'
    production_url = 'https://api.example.com'

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        return TransactionResult(
            redirect_url='https://sandbox.example.com/pay',
            form_data=None,
            method='GET',
            headers={},
        )


# -- Tests --

class TestBaseProcessorCannotInstantiate:
    def test_abstract(self):
        """BaseProcessor cannot be instantiated directly."""
        with pytest.raises(TypeError):
            BaseProcessor(ConcretePayment())


class TestBaseProcessorInit:
    def test_payment_stored(self):
        p = DummyProcessor(ConcretePayment())
        assert p.payment.id == 'pay-1'

    def test_config_default_empty(self):
        p = DummyProcessor(ConcretePayment())
        assert p.config == {}

    def test_config_provided(self):
        cfg = {'api_key': 'secret'}
        p = DummyProcessor(ConcretePayment(), config=cfg)
        assert p.config == cfg


class TestGetSetting:
    def test_returns_value(self):
        p = DummyProcessor(
            ConcretePayment(), config={'api_key': 'abc'}
        )
        assert p.get_setting('api_key') == 'abc'

    def test_returns_default_when_missing(self):
        p = DummyProcessor(ConcretePayment())
        assert p.get_setting('missing', 'fallback') == 'fallback'

    def test_returns_none_when_missing_no_default(self):
        p = DummyProcessor(ConcretePayment())
        assert p.get_setting('missing') is None


class TestGetPaywallBaseurl:
    def test_sandbox_by_default(self):
        p = DummyProcessor(ConcretePayment())
        assert p.get_paywall_baseurl() == 'https://sandbox.example.com'

    def test_sandbox_explicit(self):
        p = DummyProcessor(
            ConcretePayment(), config={'sandbox': True}
        )
        assert p.get_paywall_baseurl() == 'https://sandbox.example.com'

    def test_production(self):
        p = DummyProcessor(
            ConcretePayment(), config={'sandbox': False}
        )
        assert p.get_paywall_baseurl() == 'https://api.example.com'


class TestClassAttributes:
    def test_slug(self):
        assert DummyProcessor.slug == 'dummy'

    def test_display_name(self):
        assert DummyProcessor.display_name == 'Dummy'

    def test_accepted_currencies(self):
        assert DummyProcessor.accepted_currencies == ['PLN', 'EUR']


class TestPrepareTransaction:
    @pytest.mark.asyncio
    async def test_returns_transaction_result(self):
        p = DummyProcessor(ConcretePayment())
        result = await p.prepare_transaction()
        assert result['method'] == 'GET'
        assert result['redirect_url'] == 'https://sandbox.example.com/pay'


class TestOptionalMethodsRaiseNotImplemented:
    @pytest.mark.asyncio
    async def test_handle_callback(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.handle_callback({}, {})

    @pytest.mark.asyncio
    async def test_fetch_payment_status(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.fetch_payment_status()

    @pytest.mark.asyncio
    async def test_charge(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.charge()

    @pytest.mark.asyncio
    async def test_release_lock(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.release_lock()

    @pytest.mark.asyncio
    async def test_start_refund(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.start_refund()

    @pytest.mark.asyncio
    async def test_cancel_refund(self):
        p = DummyProcessor(ConcretePayment())
        with pytest.raises(NotImplementedError):
            await p.cancel_refund()


class TestVerifyCallbackDefault:
    @pytest.mark.asyncio
    async def test_default_is_noop(self):
        """Default verify_callback does nothing (no-op)."""
        p = DummyProcessor(ConcretePayment())
        result = await p.verify_callback({}, {})
        assert result is None
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_processor.py -v
```

Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `src/getpaid_core/processor.py`:

```python
"""Base payment processor abstract class.

All payment backends subclass BaseProcessor and implement at minimum
prepare_transaction(). Other methods are optional depending on the
payment gateway's capabilities.
"""

from abc import ABC
from abc import abstractmethod
from decimal import Decimal

from getpaid_core.protocols import Payment
from getpaid_core.types import ChargeResponse
from getpaid_core.types import PaymentStatusResponse
from getpaid_core.types import TransactionResult


class BaseProcessor(ABC):
    """Base class for payment backend processors."""

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

    def get_setting(self, name: str, default=None):
        """Read a setting from backend config."""
        return self.config.get(name, default)

    def get_paywall_baseurl(self) -> str:
        """Return sandbox or production URL based on config."""
        sandbox = self.get_setting('sandbox', True)
        return self.sandbox_url if sandbox else self.production_url

    @abstractmethod
    async def prepare_transaction(
        self, **kwargs
    ) -> TransactionResult:
        """Prepare data for initiating a payment.

        Returns a TransactionResult that the framework adapter
        converts into an HTTP response.
        """
        ...

    async def verify_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        """Verify callback authenticity.

        Raise GetPaidException to reject. Default: no-op.
        """

    async def handle_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        """Handle async PUSH callback from payment gateway."""
        raise NotImplementedError

    async def fetch_payment_status(
        self, **kwargs
    ) -> PaymentStatusResponse:
        """PULL flow: fetch payment status from gateway."""
        raise NotImplementedError

    async def charge(
        self, amount: Decimal | None = None, **kwargs
    ) -> ChargeResponse:
        """Charge a pre-authorized payment."""
        raise NotImplementedError

    async def release_lock(self, **kwargs) -> Decimal:
        """Release pre-authorized lock. Return locked amount."""
        raise NotImplementedError

    async def start_refund(
        self, amount: Decimal | None = None, **kwargs
    ) -> Decimal:
        """Start a refund. Return refund amount."""
        raise NotImplementedError

    async def cancel_refund(self, **kwargs) -> bool:
        """Cancel in-progress refund. Return True if ok."""
        raise NotImplementedError
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_processor.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/getpaid_core/processor.py tests/test_processor.py
git commit -m "feat: add BaseProcessor ABC"
```

---

## Phase 3: State Machine

### Task 7: FSM with transitions library

**Files:**
- Create: `src/getpaid_core/fsm.py`
- Modify: `tests/test_flow.py` -> rename to `tests/test_fsm.py` (replace stub)

This is the most complex module. It defines all valid payment and fraud status
transitions and the `ALLOWED_CALLBACKS` security frozenset.

**Reference:** The existing FSM transitions are defined in
`/home/minder/projekty/django-getpaid/django-getpaid/getpaid/abstracts.py` lines 439-631.
Our transitions must produce identical state changes.

**Step 1: Write the failing test**

Delete the existing stub `tests/test_flow.py` and create `tests/test_fsm.py`:

```python
"""Tests for getpaid_core.fsm -- payment state machine."""

import pytest

from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus
from getpaid_core.fsm import ALLOWED_CALLBACKS
from getpaid_core.fsm import create_fraud_machine
from getpaid_core.fsm import create_payment_machine


class MockPayment:
    """Minimal object for FSM attachment."""

    def __init__(
        self,
        status=PaymentStatus.NEW,
        fraud_status=FraudStatus.UNKNOWN,
    ):
        self.status = status
        self.fraud_status = fraud_status
        self.amount_required = 100
        self.amount_paid = 0
        self.amount_refunded = 0

    def is_fully_paid(self):
        return self.amount_paid >= self.amount_required

    def is_fully_refunded(self):
        return self.amount_refunded >= self.amount_paid


# === Payment FSM: valid transitions ===

class TestPaymentPrepare:
    def test_new_to_prepared(self):
        p = MockPayment(status=PaymentStatus.NEW)
        create_payment_machine(p)
        p.confirm_prepared()
        assert p.status == PaymentStatus.PREPARED


class TestPaymentLock:
    def test_new_to_pre_auth(self):
        p = MockPayment(status=PaymentStatus.NEW)
        create_payment_machine(p)
        p.confirm_lock()
        assert p.status == PaymentStatus.PRE_AUTH

    def test_prepared_to_pre_auth(self):
        p = MockPayment(status=PaymentStatus.PREPARED)
        create_payment_machine(p)
        p.confirm_lock()
        assert p.status == PaymentStatus.PRE_AUTH


class TestPaymentCharge:
    def test_pre_auth_to_in_charge(self):
        p = MockPayment(status=PaymentStatus.PRE_AUTH)
        create_payment_machine(p)
        p.confirm_charge_sent()
        assert p.status == PaymentStatus.IN_CHARGE


class TestPaymentConfirmPayment:
    def test_pre_auth_to_partial(self):
        p = MockPayment(status=PaymentStatus.PRE_AUTH)
        create_payment_machine(p)
        p.confirm_payment()
        assert p.status == PaymentStatus.PARTIAL

    def test_prepared_to_partial(self):
        p = MockPayment(status=PaymentStatus.PREPARED)
        create_payment_machine(p)
        p.confirm_payment()
        assert p.status == PaymentStatus.PARTIAL

    def test_in_charge_to_partial(self):
        p = MockPayment(status=PaymentStatus.IN_CHARGE)
        create_payment_machine(p)
        p.confirm_payment()
        assert p.status == PaymentStatus.PARTIAL

    def test_partial_stays_partial(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        create_payment_machine(p)
        p.confirm_payment()
        assert p.status == PaymentStatus.PARTIAL


class TestPaymentMarkAsPaid:
    def test_partial_to_paid_when_fully_paid(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        p.amount_paid = 100
        create_payment_machine(p)
        p.mark_as_paid()
        assert p.status == PaymentStatus.PAID

    def test_partial_to_paid_blocked_when_not_fully_paid(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        p.amount_paid = 50
        create_payment_machine(p)
        # transitions raises MachineError when condition fails
        from transitions.core import MachineError
        with pytest.raises(MachineError):
            p.mark_as_paid()
        assert p.status == PaymentStatus.PARTIAL


class TestPaymentReleaseLock:
    def test_pre_auth_to_refunded(self):
        p = MockPayment(status=PaymentStatus.PRE_AUTH)
        create_payment_machine(p)
        p.release_lock()
        assert p.status == PaymentStatus.REFUNDED


class TestPaymentRefundFlow:
    def test_paid_to_refund_started(self):
        p = MockPayment(status=PaymentStatus.PAID)
        create_payment_machine(p)
        p.start_refund()
        assert p.status == PaymentStatus.REFUND_STARTED

    def test_partial_to_refund_started(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        create_payment_machine(p)
        p.start_refund()
        assert p.status == PaymentStatus.REFUND_STARTED

    def test_cancel_refund_to_partial(self):
        p = MockPayment(status=PaymentStatus.REFUND_STARTED)
        create_payment_machine(p)
        p.cancel_refund()
        assert p.status == PaymentStatus.PARTIAL

    def test_confirm_refund_to_partial(self):
        p = MockPayment(status=PaymentStatus.REFUND_STARTED)
        create_payment_machine(p)
        p.confirm_refund()
        assert p.status == PaymentStatus.PARTIAL

    def test_mark_as_refunded_when_fully_refunded(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        p.amount_paid = 100
        p.amount_refunded = 100
        create_payment_machine(p)
        p.mark_as_refunded()
        assert p.status == PaymentStatus.REFUNDED

    def test_mark_as_refunded_blocked_when_not_fully(self):
        p = MockPayment(status=PaymentStatus.PARTIAL)
        p.amount_paid = 100
        p.amount_refunded = 50
        create_payment_machine(p)
        from transitions.core import MachineError
        with pytest.raises(MachineError):
            p.mark_as_refunded()


class TestPaymentFail:
    def test_new_to_failed(self):
        p = MockPayment(status=PaymentStatus.NEW)
        create_payment_machine(p)
        p.fail()
        assert p.status == PaymentStatus.FAILED

    def test_pre_auth_to_failed(self):
        p = MockPayment(status=PaymentStatus.PRE_AUTH)
        create_payment_machine(p)
        p.fail()
        assert p.status == PaymentStatus.FAILED

    def test_prepared_to_failed(self):
        p = MockPayment(status=PaymentStatus.PREPARED)
        create_payment_machine(p)
        p.fail()
        assert p.status == PaymentStatus.FAILED


# === Payment FSM: invalid transitions ===

class TestPaymentInvalidTransitions:
    def test_paid_cannot_fail(self):
        p = MockPayment(status=PaymentStatus.PAID)
        create_payment_machine(p)
        from transitions.core import MachineError
        with pytest.raises(MachineError):
            p.fail()

    def test_failed_cannot_prepare(self):
        p = MockPayment(status=PaymentStatus.FAILED)
        create_payment_machine(p)
        from transitions.core import MachineError
        with pytest.raises(MachineError):
            p.confirm_prepared()

    def test_refunded_cannot_charge(self):
        p = MockPayment(status=PaymentStatus.REFUNDED)
        create_payment_machine(p)
        from transitions.core import MachineError
        with pytest.raises(MachineError):
            p.confirm_charge_sent()


# === Fraud FSM ===

class TestFraudFSM:
    def test_unknown_to_rejected(self):
        p = MockPayment()
        create_fraud_machine(p)
        p.flag_as_fraud()
        assert p.fraud_status == FraudStatus.REJECTED

    def test_unknown_to_accepted(self):
        p = MockPayment()
        create_fraud_machine(p)
        p.flag_as_legit()
        assert p.fraud_status == FraudStatus.ACCEPTED

    def test_unknown_to_check(self):
        p = MockPayment()
        create_fraud_machine(p)
        p.flag_for_check()
        assert p.fraud_status == FraudStatus.CHECK

    def test_check_to_rejected(self):
        p = MockPayment(fraud_status=FraudStatus.CHECK)
        create_fraud_machine(p)
        p.mark_as_fraud()
        assert p.fraud_status == FraudStatus.REJECTED

    def test_check_to_accepted(self):
        p = MockPayment(fraud_status=FraudStatus.CHECK)
        create_fraud_machine(p)
        p.mark_as_legit()
        assert p.fraud_status == FraudStatus.ACCEPTED


# === ALLOWED_CALLBACKS ===

class TestAllowedCallbacks:
    def test_is_frozenset(self):
        assert isinstance(ALLOWED_CALLBACKS, frozenset)

    def test_contains_all_payment_triggers(self):
        expected = {
            'confirm_prepared', 'confirm_lock',
            'confirm_charge_sent', 'confirm_payment',
            'mark_as_paid', 'release_lock',
            'start_refund', 'cancel_refund',
            'confirm_refund', 'mark_as_refunded', 'fail',
        }
        assert ALLOWED_CALLBACKS == expected

    def test_does_not_contain_fraud_triggers(self):
        """Fraud triggers should not be externally invocable."""
        assert 'flag_as_fraud' not in ALLOWED_CALLBACKS
        assert 'flag_as_legit' not in ALLOWED_CALLBACKS
```

**Step 2: Run test to verify it fails**

```bash
rm tests/test_flow.py
uv run pytest tests/test_fsm.py -v
```

Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `src/getpaid_core/fsm.py`:

```python
"""Payment state machine using the transitions library.

Defines all valid payment and fraud status transitions.
The machine attaches trigger methods directly to payment objects.
"""

from transitions import Machine

from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus


PAYMENT_TRANSITIONS = [
    {
        'trigger': 'confirm_prepared',
        'source': PaymentStatus.NEW,
        'dest': PaymentStatus.PREPARED,
    },
    {
        'trigger': 'confirm_lock',
        'source': [PaymentStatus.NEW, PaymentStatus.PREPARED],
        'dest': PaymentStatus.PRE_AUTH,
    },
    {
        'trigger': 'confirm_charge_sent',
        'source': PaymentStatus.PRE_AUTH,
        'dest': PaymentStatus.IN_CHARGE,
    },
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
    {
        'trigger': 'mark_as_paid',
        'source': PaymentStatus.PARTIAL,
        'dest': PaymentStatus.PAID,
        'conditions': ['is_fully_paid'],
    },
    {
        'trigger': 'release_lock',
        'source': PaymentStatus.PRE_AUTH,
        'dest': PaymentStatus.REFUNDED,
    },
    {
        'trigger': 'start_refund',
        'source': [
            PaymentStatus.PAID,
            PaymentStatus.PARTIAL,
        ],
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

    The transitions library adds trigger methods directly to the
    object (confirm_prepared, confirm_lock, fail, etc.).
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

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_fsm.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/getpaid_core/fsm.py tests/test_fsm.py
git rm tests/test_flow.py 2>/dev/null; true
git commit -m "feat: add payment/fraud FSM using transitions library"
```

---

## Phase 4: Registry

### Task 8: Plugin Registry

**Files:**
- Create: `src/getpaid_core/registry.py`
- Modify: `tests/test_registry.py` (replace empty stub)

**Step 1: Write the failing test**

Write `tests/test_registry.py`:

```python
"""Tests for getpaid_core.registry.PluginRegistry."""

from decimal import Decimal
from unittest.mock import patch

import pytest

from getpaid_core.processor import BaseProcessor
from getpaid_core.registry import ENTRY_POINT_GROUP
from getpaid_core.registry import PluginRegistry
from getpaid_core.types import TransactionResult


# -- Test processors --

class PLNProcessor(BaseProcessor):
    slug = 'pln-pay'
    display_name = 'PLN Payments'
    accepted_currencies = ['PLN']

    async def prepare_transaction(self, **kwargs):
        return TransactionResult(
            redirect_url=None, form_data=None,
            method='REST', headers={},
        )


class MultiProcessor(BaseProcessor):
    slug = 'multi-pay'
    display_name = 'Multi Payments'
    accepted_currencies = ['PLN', 'EUR', 'USD']

    async def prepare_transaction(self, **kwargs):
        return TransactionResult(
            redirect_url=None, form_data=None,
            method='REST', headers={},
        )


class EURProcessor(BaseProcessor):
    slug = 'eur-pay'
    display_name = 'EUR Payments'
    accepted_currencies = ['EUR']

    async def prepare_transaction(self, **kwargs):
        return TransactionResult(
            redirect_url=None, form_data=None,
            method='REST', headers={},
        )


# -- Tests --

class TestManualRegistration:
    def test_register(self):
        reg = PluginRegistry()
        reg.register(PLNProcessor)
        assert reg.get_by_slug('pln-pay') is PLNProcessor

    def test_register_multiple(self):
        reg = PluginRegistry()
        reg.register(PLNProcessor)
        reg.register(EURProcessor)
        assert reg.get_by_slug('pln-pay') is PLNProcessor
        assert reg.get_by_slug('eur-pay') is EURProcessor

    def test_unregister(self):
        reg = PluginRegistry()
        reg.register(PLNProcessor)
        reg.unregister('pln-pay')
        with pytest.raises(KeyError):
            reg.get_by_slug('pln-pay')

    def test_unregister_nonexistent_silent(self):
        reg = PluginRegistry()
        reg.unregister('nonexistent')  # should not raise


class TestGetBySlug:
    def test_unknown_slug_raises(self):
        reg = PluginRegistry()
        reg._discovered = True  # skip entry_point discovery
        with pytest.raises(KeyError):
            reg.get_by_slug('nonexistent')


class TestGetForCurrency:
    def test_single_match(self):
        reg = PluginRegistry()
        reg._discovered = True
        reg.register(PLNProcessor)
        reg.register(EURProcessor)
        result = reg.get_for_currency('PLN')
        assert result == [PLNProcessor]

    def test_multiple_matches(self):
        reg = PluginRegistry()
        reg._discovered = True
        reg.register(PLNProcessor)
        reg.register(MultiProcessor)
        result = reg.get_for_currency('PLN')
        assert set(result) == {PLNProcessor, MultiProcessor}

    def test_no_matches(self):
        reg = PluginRegistry()
        reg._discovered = True
        reg.register(PLNProcessor)
        result = reg.get_for_currency('GBP')
        assert result == []


class TestGetChoices:
    def test_returns_tuples(self):
        reg = PluginRegistry()
        reg._discovered = True
        reg.register(PLNProcessor)
        choices = reg.get_choices('PLN')
        assert choices == [('pln-pay', 'PLN Payments')]

    def test_empty_for_unknown_currency(self):
        reg = PluginRegistry()
        reg._discovered = True
        choices = reg.get_choices('GBP')
        assert choices == []


class TestGetAllCurrencies:
    def test_union_of_all(self):
        reg = PluginRegistry()
        reg._discovered = True
        reg.register(PLNProcessor)
        reg.register(EURProcessor)
        reg.register(MultiProcessor)
        currencies = reg.get_all_currencies()
        assert currencies == {'PLN', 'EUR', 'USD'}


class TestEntryPointDiscovery:
    def test_entry_point_group_constant(self):
        assert ENTRY_POINT_GROUP == 'getpaid.backends'

    def test_auto_discover_on_first_access(self):
        """Registry auto-discovers on first query if not yet done."""
        reg = PluginRegistry()
        assert reg._discovered is False
        # Calling get_for_currency triggers discovery
        with patch.object(reg, 'discover') as mock_discover:
            reg.get_for_currency('PLN')
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
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_registry.py -v
```

Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `src/getpaid_core/registry.py`:

```python
"""Plugin registry for payment backends.

Primary discovery via entry_points. Manual registration for
testing and dynamic scenarios.
"""

from importlib.metadata import entry_points

from getpaid_core.processor import BaseProcessor


ENTRY_POINT_GROUP = 'getpaid.backends'


class PluginRegistry:
    """Discovers and stores payment backend processors."""

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
                self._backends[processor_class.slug] = (
                    processor_class
                )
        self._discovered = True

    def register(
        self, processor_class: type[BaseProcessor]
    ) -> None:
        """Manual registration for testing or dynamic use."""
        self._backends[processor_class.slug] = processor_class

    def unregister(self, slug: str) -> None:
        """Remove a backend by slug."""
        self._backends.pop(slug, None)

    def get_for_currency(
        self, currency: str
    ) -> list[type[BaseProcessor]]:
        """Return all backends supporting the given currency."""
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

    def get_by_slug(
        self, slug: str
    ) -> type[BaseProcessor]:
        """Return a backend class by slug. Raises KeyError."""
        self._ensure_discovered()
        return self._backends[slug]

    def get_all_currencies(self) -> set[str]:
        """Return all currencies supported by all backends."""
        self._ensure_discovered()
        currencies: set[str] = set()
        for b in self._backends.values():
            currencies.update(b.accepted_currencies)
        return currencies

    def _ensure_discovered(self) -> None:
        if not self._discovered:
            self.discover()


registry = PluginRegistry()
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_registry.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/getpaid_core/registry.py tests/test_registry.py
git commit -m "feat: add plugin registry with entry_point discovery"
```

---

## Phase 5: Validators & Flow

### Task 9: Validators

**Files:**
- Create: `src/getpaid_core/validators.py`
- Create: `tests/test_validators.py`

The validator system runs a chain of validator functions on payment data before
processing. Unlike django-getpaid which reads validators from Django settings,
getpaid-core accepts validators as a list of callables.

**Step 1: Write the failing test**

Write `tests/test_validators.py`:

```python
"""Tests for getpaid_core.validators."""

import pytest

from getpaid_core.exceptions import GetPaidException
from getpaid_core.validators import run_validators


class TestRunValidators:
    def test_no_validators(self):
        """No validators means no error."""
        run_validators({'amount': 100}, validators=[])

    def test_single_passing_validator(self):
        def ok_validator(data):
            return data

        run_validators({'amount': 100}, validators=[ok_validator])

    def test_validator_can_modify_data(self):
        """Validators receive and return data (pipeline)."""
        def add_field(data):
            data['extra'] = True
            return data

        data = {'amount': 100}
        result = run_validators(data, validators=[add_field])
        assert result['extra'] is True

    def test_chain_of_validators(self):
        def add_a(data):
            data['a'] = 1
            return data

        def add_b(data):
            data['b'] = 2
            return data

        result = run_validators({}, validators=[add_a, add_b])
        assert result == {'a': 1, 'b': 2}

    def test_failing_validator_raises(self):
        def fail_validator(data):
            raise GetPaidException('invalid payment')

        with pytest.raises(GetPaidException, match='invalid payment'):
            run_validators({}, validators=[fail_validator])

    def test_validators_run_in_order(self):
        order = []

        def first(data):
            order.append(1)
            return data

        def second(data):
            order.append(2)
            return data

        run_validators({}, validators=[first, second])
        assert order == [1, 2]

    def test_default_no_validators(self):
        """Called with no validators argument defaults to empty."""
        result = run_validators({'x': 1})
        assert result == {'x': 1}
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_validators.py -v
```

Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `src/getpaid_core/validators.py`:

```python
"""Pluggable payment validation system.

Validators are callables that receive a data dict, optionally
modify it, and return it. They raise GetPaidException to reject.
"""

from collections.abc import Callable


def run_validators(
    data: dict,
    validators: list[Callable] | None = None,
) -> dict:
    """Run a chain of validators on payment data.

    Each validator receives the data dict and must return it
    (possibly modified). Raise GetPaidException to reject.
    """
    for validator in validators or []:
        data = validator(data)
    return data
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_validators.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/getpaid_core/validators.py tests/test_validators.py
git commit -m "feat: add pluggable validator system"
```

---

### Task 10: PaymentFlow Orchestrator

**Files:**
- Create: `src/getpaid_core/flow.py`
- Create: `tests/test_flow.py`
- Create: `tests/conftest.py` (shared fixtures)

This is the central orchestrator that framework adapters interact with.
It depends on all previous modules.

**Step 1: Write shared test fixtures**

Create `tests/conftest.py`:

```python
"""Shared test fixtures for getpaid-core."""

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus
from getpaid_core.processor import BaseProcessor
from getpaid_core.registry import PluginRegistry
from getpaid_core.types import TransactionResult


class MockOrder:
    """A mock order satisfying the Order protocol."""

    def __init__(self, total=Decimal('100.00'), currency='PLN'):
        self._total = total
        self._currency = currency

    def get_total_amount(self):
        return self._total

    def get_buyer_info(self):
        return {'email': 'test@example.com'}

    def get_description(self):
        return 'Test order'

    def get_currency(self):
        return self._currency

    def get_items(self):
        return []

    def get_return_url(self, success=None):
        return '/return/'


class MockPayment:
    """A mock payment satisfying the Payment protocol."""

    def __init__(self, **kwargs):
        self.id = kwargs.get('id', 'pay-1')
        self.order = kwargs.get('order', MockOrder())
        self.amount_required = kwargs.get(
            'amount_required', Decimal('100.00')
        )
        self.currency = kwargs.get('currency', 'PLN')
        self.status = kwargs.get('status', PaymentStatus.NEW)
        self.backend = kwargs.get('backend', 'mock')
        self.external_id = kwargs.get('external_id', '')
        self.description = kwargs.get('description', 'Test')
        self.amount_paid = kwargs.get(
            'amount_paid', Decimal('0')
        )
        self.amount_locked = kwargs.get(
            'amount_locked', Decimal('0')
        )
        self.amount_refunded = kwargs.get(
            'amount_refunded', Decimal('0')
        )
        self.fraud_status = kwargs.get(
            'fraud_status', FraudStatus.UNKNOWN
        )
        self.fraud_message = kwargs.get('fraud_message', '')

    def is_fully_paid(self):
        return self.amount_paid >= self.amount_required

    def is_fully_refunded(self):
        return self.amount_refunded >= self.amount_paid


class MockProcessor(BaseProcessor):
    """A mock processor for testing PaymentFlow."""

    slug = 'mock'
    display_name = 'Mock'
    accepted_currencies = ['PLN', 'EUR']

    async def prepare_transaction(self, **kwargs):
        return TransactionResult(
            redirect_url='https://mock.example.com/pay',
            form_data=None,
            method='GET',
            headers={},
        )

    async def handle_callback(self, data, headers, **kwargs):
        """Apply the status from callback data."""
        status = data.get('status')
        if status and hasattr(self.payment, status):
            trigger = getattr(self.payment, status)
            if callable(trigger):
                trigger()

    async def fetch_payment_status(self, **kwargs):
        return {'status': 'confirm_payment'}


class MockRepository:
    """In-memory repository for testing."""

    def __init__(self):
        self._payments = {}

    async def get_by_id(self, payment_id):
        return self._payments[payment_id]

    async def create(self, **kwargs):
        payment = MockPayment(**kwargs)
        self._payments[payment.id] = payment
        return payment

    async def save(self, payment):
        self._payments[payment.id] = payment
        return payment

    async def update_status(self, payment_id, status, **fields):
        payment = self._payments[payment_id]
        payment.status = status
        for k, v in fields.items():
            setattr(payment, k, v)
        return payment

    async def list_by_order(self, order_id):
        return list(self._payments.values())


@pytest.fixture
def mock_registry():
    """A fresh registry with MockProcessor registered."""
    reg = PluginRegistry()
    reg._discovered = True
    reg.register(MockProcessor)
    return reg


@pytest.fixture
def mock_repo():
    return MockRepository()
```

**Step 2: Write the failing test**

Create `tests/test_flow.py`:

```python
"""Tests for getpaid_core.flow.PaymentFlow."""

from decimal import Decimal
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.flow import PaymentFlow
from tests.conftest import MockOrder
from tests.conftest import MockPayment
from tests.conftest import MockProcessor
from tests.conftest import MockRepository


@pytest.fixture
def flow(mock_repo, mock_registry):
    """PaymentFlow with mock repo and registry."""
    with patch('getpaid_core.flow.registry', mock_registry):
        yield PaymentFlow(
            repository=mock_repo,
            config={'mock': {'sandbox': True}},
        )


class TestCreatePayment:
    @pytest.mark.asyncio
    async def test_creates_payment(self, flow, mock_repo):
        order = MockOrder()
        payment = await flow.create_payment(order, 'mock')
        assert payment.backend == 'mock'
        assert payment.amount_required == Decimal('100.00')
        assert payment.currency == 'PLN'

    @pytest.mark.asyncio
    async def test_unknown_backend_raises(self, flow):
        order = MockOrder()
        with pytest.raises(KeyError):
            await flow.create_payment(order, 'nonexistent')


class TestPrepare:
    @pytest.mark.asyncio
    async def test_prepare_returns_transaction_result(self, flow):
        payment = MockPayment(backend='mock')
        result = await flow.prepare(payment)
        assert result['method'] == 'GET'
        assert result['redirect_url'] == (
            'https://mock.example.com/pay'
        )

    @pytest.mark.asyncio
    async def test_prepare_transitions_to_prepared(self, flow):
        payment = MockPayment(
            backend='mock', status=PaymentStatus.NEW
        )
        await flow.prepare(payment)
        assert payment.status == PaymentStatus.PREPARED

    @pytest.mark.asyncio
    async def test_prepare_saves_payment(
        self, flow, mock_repo
    ):
        payment = MockPayment(backend='mock')
        mock_repo._payments[payment.id] = payment
        await flow.prepare(payment)
        saved = await mock_repo.get_by_id(payment.id)
        assert saved.status == PaymentStatus.PREPARED


class TestHandleCallback:
    @pytest.mark.asyncio
    async def test_callback_applies_status(self, flow):
        payment = MockPayment(
            backend='mock', status=PaymentStatus.PREPARED
        )
        await flow.handle_callback(
            payment,
            data={'status': 'confirm_payment'},
            headers={},
        )
        assert payment.status == PaymentStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_callback_saves(self, flow, mock_repo):
        payment = MockPayment(
            backend='mock', status=PaymentStatus.PREPARED
        )
        mock_repo._payments[payment.id] = payment
        await flow.handle_callback(
            payment, data={'status': 'confirm_payment'},
            headers={},
        )
        saved = await mock_repo.get_by_id(payment.id)
        assert saved.status == PaymentStatus.PARTIAL


class TestFetchAndUpdateStatus:
    @pytest.mark.asyncio
    async def test_pull_updates_status(self, flow):
        payment = MockPayment(
            backend='mock', status=PaymentStatus.PREPARED
        )
        result = await flow.fetch_and_update_status(payment)
        assert result.status == PaymentStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_pull_disallowed_callback(self, flow):
        payment = MockPayment(
            backend='mock', status=PaymentStatus.PREPARED
        )
        # Patch processor to return a disallowed callback
        with patch.object(
            MockProcessor, 'fetch_payment_status',
            new_callable=AsyncMock,
            return_value={'status': 'flag_as_fraud'},
        ):
            with pytest.raises(InvalidTransitionError):
                await flow.fetch_and_update_status(payment)


class TestCharge:
    @pytest.mark.asyncio
    async def test_charge_transitions_on_success(self, flow):
        payment = MockPayment(
            backend='mock', status=PaymentStatus.PRE_AUTH
        )
        with patch.object(
            MockProcessor, 'charge',
            new_callable=AsyncMock,
            return_value={
                'amount_charged': Decimal('100'),
                'success': True,
                'async_call': False,
            },
        ):
            result = await flow.charge(payment)
        assert result['success'] is True
        assert payment.status == PaymentStatus.IN_CHARGE


class TestReleaseLock:
    @pytest.mark.asyncio
    async def test_release_lock(self, flow):
        payment = MockPayment(
            backend='mock', status=PaymentStatus.PRE_AUTH
        )
        with patch.object(
            MockProcessor, 'release_lock',
            new_callable=AsyncMock,
            return_value=Decimal('100'),
        ):
            amount = await flow.release_lock(payment)
        assert amount == Decimal('100')
        assert payment.status == PaymentStatus.REFUNDED


class TestStartRefund:
    @pytest.mark.asyncio
    async def test_start_refund(self, flow):
        payment = MockPayment(
            backend='mock', status=PaymentStatus.PAID
        )
        with patch.object(
            MockProcessor, 'start_refund',
            new_callable=AsyncMock,
            return_value=Decimal('50'),
        ):
            amount = await flow.start_refund(payment)
        assert amount == Decimal('50')
        assert payment.status == PaymentStatus.REFUND_STARTED


class TestCancelRefund:
    @pytest.mark.asyncio
    async def test_cancel_refund_success(self, flow):
        payment = MockPayment(
            backend='mock',
            status=PaymentStatus.REFUND_STARTED,
        )
        with patch.object(
            MockProcessor, 'cancel_refund',
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await flow.cancel_refund(payment)
        assert result is True
        assert payment.status == PaymentStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_cancel_refund_failure_no_transition(
        self, flow
    ):
        payment = MockPayment(
            backend='mock',
            status=PaymentStatus.REFUND_STARTED,
        )
        with patch.object(
            MockProcessor, 'cancel_refund',
            new_callable=AsyncMock,
            return_value=False,
        ):
            result = await flow.cancel_refund(payment)
        assert result is False
        assert payment.status == PaymentStatus.REFUND_STARTED
```

**Step 3: Run test to verify it fails**

```bash
uv run pytest tests/test_flow.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'getpaid_core.flow'`

**Step 4: Write the implementation**

Create `src/getpaid_core/flow.py`:

```python
"""Payment flow orchestrator.

The main entry point for framework adapters. Orchestrates the
interaction between repository, processor, and state machine.
"""

from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.fsm import ALLOWED_CALLBACKS
from getpaid_core.fsm import create_fraud_machine
from getpaid_core.fsm import create_payment_machine
from getpaid_core.protocols import Order
from getpaid_core.protocols import Payment
from getpaid_core.protocols import PaymentRepository
from getpaid_core.registry import registry
from getpaid_core.types import TransactionResult
from getpaid_core.validators import run_validators


class PaymentFlow:
    """Core payment processing orchestrator.

    Framework adapters create an instance with their repository
    and backend configuration, then delegate to its methods.
    """

    def __init__(
        self,
        repository: PaymentRepository,
        config: dict | None = None,
        validators: list | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or {}
        self.validators = validators or []

    async def create_payment(
        self, order: Order, backend_slug: str, **kwargs
    ) -> Payment:
        """Create a new payment for an order."""
        registry.get_by_slug(backend_slug)
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
        transitions to PREPARED, and persists.
        """
        run_validators(
            {'payment': payment}, validators=self.validators
        )
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
        """Handle an incoming PUSH callback from the gateway."""
        processor = self._get_processor(payment)
        await processor.verify_callback(data, headers, **kwargs)
        create_payment_machine(payment)
        create_fraud_machine(payment)
        await processor.handle_callback(
            data, headers, **kwargs
        )
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
            if callback not in ALLOWED_CALLBACKS:
                raise InvalidTransitionError(
                    f'Callback {callback!r} not in'
                    ' ALLOWED_CALLBACKS'
                )
            trigger = getattr(payment, callback, None)
            if trigger and callable(trigger):
                trigger()
        await self.repository.save(payment)
        return payment

    async def charge(
        self, payment: Payment, amount=None, **kwargs
    ):
        """Charge a pre-authorized payment."""
        processor = self._get_processor(payment)
        create_payment_machine(payment)
        result = await processor.charge(
            amount=amount, **kwargs
        )
        if result['success']:
            payment.confirm_charge_sent()
        await self.repository.save(payment)
        return result

    async def release_lock(
        self, payment: Payment, **kwargs
    ):
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

    async def cancel_refund(
        self, payment: Payment, **kwargs
    ):
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
        backend_config = self.config.get(
            payment.backend, {}
        )
        return processor_class(payment, config=backend_config)
```

**Step 5: Run test to verify it passes**

```bash
uv run pytest tests/test_flow.py -v
```

Expected: all PASS

**Step 6: Commit**

```bash
git add src/getpaid_core/flow.py tests/test_flow.py tests/conftest.py
git commit -m "feat: add PaymentFlow orchestrator and test fixtures"
```

---

## Phase 6: Public API & Cleanup

### Task 11: Public API Exports

**Files:**
- Modify: `src/getpaid_core/__init__.py`
- Delete: `tests/test_main.py` (obsolete CLI test stub)

**Step 1: Update __init__.py with public exports**

Replace `src/getpaid_core/__init__.py` with:

```python
"""Getpaid Core -- framework-agnostic payment processing."""

__version__ = '0.1.0'

from getpaid_core.enums import BackendMethod
from getpaid_core.enums import ConfirmationMethod
from getpaid_core.enums import FraudStatus
from getpaid_core.enums import PaymentStatus
from getpaid_core.exceptions import ChargeFailure
from getpaid_core.exceptions import CommunicationError
from getpaid_core.exceptions import CredentialsError
from getpaid_core.exceptions import GetPaidException
from getpaid_core.exceptions import InvalidCallbackError
from getpaid_core.exceptions import InvalidTransitionError
from getpaid_core.exceptions import LockFailure
from getpaid_core.exceptions import RefundFailure
from getpaid_core.flow import PaymentFlow
from getpaid_core.processor import BaseProcessor
from getpaid_core.registry import registry

__all__ = [
    '__version__',
    'BackendMethod',
    'BaseProcessor',
    'ChargeFailure',
    'CommunicationError',
    'ConfirmationMethod',
    'CredentialsError',
    'FraudStatus',
    'GetPaidException',
    'InvalidCallbackError',
    'InvalidTransitionError',
    'LockFailure',
    'PaymentFlow',
    'PaymentStatus',
    'RefundFailure',
    'registry',
]
```

**Step 2: Delete obsolete files**

```bash
rm tests/test_main.py
```

**Step 3: Write import smoke test**

Create `tests/test_public_api.py`:

```python
"""Tests for the public API surface of getpaid_core."""

import getpaid_core


class TestPublicAPI:
    def test_version(self):
        assert getpaid_core.__version__ == '0.1.0'

    def test_exports_enums(self):
        assert getpaid_core.PaymentStatus is not None
        assert getpaid_core.FraudStatus is not None
        assert getpaid_core.BackendMethod is not None
        assert getpaid_core.ConfirmationMethod is not None

    def test_exports_exceptions(self):
        assert getpaid_core.GetPaidException is not None
        assert getpaid_core.CommunicationError is not None
        assert getpaid_core.ChargeFailure is not None
        assert getpaid_core.LockFailure is not None
        assert getpaid_core.RefundFailure is not None
        assert getpaid_core.CredentialsError is not None
        assert getpaid_core.InvalidCallbackError is not None
        assert getpaid_core.InvalidTransitionError is not None

    def test_exports_core_classes(self):
        assert getpaid_core.BaseProcessor is not None
        assert getpaid_core.PaymentFlow is not None

    def test_exports_registry(self):
        assert getpaid_core.registry is not None

    def test_all_is_defined(self):
        assert hasattr(getpaid_core, '__all__')
        assert len(getpaid_core.__all__) > 0
```

**Step 4: Run full test suite**

```bash
uv run pytest -v
```

Expected: all tests PASS across all test files

**Step 5: Commit**

```bash
git add -A
git commit -m "feat: define public API exports, remove CLI stub"
```

---

### Task 12: Run Full Suite & Lint

**Step 1: Run all tests with coverage**

```bash
uv run pytest --cov=getpaid_core --cov-report=term-missing -v
```

Expected: all PASS, good coverage

**Step 2: Run ruff lint**

```bash
uv run ruff check src/ tests/
```

Fix any issues found.

**Step 3: Run ruff format**

```bash
uv run ruff format src/ tests/
```

**Step 4: Commit any lint fixes**

```bash
git add -A
git commit -m "style: fix lint and formatting issues"
```

---

## Phase 7: Dummy Backend (reference implementation)

### Task 13: Built-in Dummy Processor

**Files:**
- Create: `src/getpaid_core/backends/__init__.py`
- Create: `src/getpaid_core/backends/dummy.py`
- Create: `tests/test_dummy_backend.py`

The dummy backend is a complete reference implementation that makes zero HTTP
calls. It's useful for development, testing, and as documentation for backend
authors.

**Reference:** `/home/minder/projekty/django-getpaid/django-getpaid/getpaid/backends/dummy/processor.py`

**Step 1: Write the failing test**

Write `tests/test_dummy_backend.py`:

```python
"""Tests for the built-in dummy payment backend."""

from decimal import Decimal

import pytest

from getpaid_core.backends.dummy import DummyProcessor
from getpaid_core.enums import PaymentStatus
from getpaid_core.fsm import create_payment_machine
from getpaid_core.processor import BaseProcessor
from tests.conftest import MockPayment


class TestDummyProcessorAttributes:
    def test_is_base_processor(self):
        assert issubclass(DummyProcessor, BaseProcessor)

    def test_slug(self):
        assert DummyProcessor.slug == 'dummy'

    def test_display_name(self):
        assert DummyProcessor.display_name == 'Dummy'

    def test_accepted_currencies(self):
        # Dummy accepts all common currencies
        assert 'PLN' in DummyProcessor.accepted_currencies
        assert 'EUR' in DummyProcessor.accepted_currencies
        assert 'USD' in DummyProcessor.accepted_currencies


class TestDummyPrepareTransaction:
    @pytest.mark.asyncio
    async def test_get_method(self):
        payment = MockPayment(backend='dummy')
        proc = DummyProcessor(
            payment, config={'method': 'GET'}
        )
        result = await proc.prepare_transaction()
        assert result['method'] == 'GET'
        assert result['redirect_url'] is not None

    @pytest.mark.asyncio
    async def test_post_method(self):
        payment = MockPayment(backend='dummy')
        proc = DummyProcessor(
            payment, config={'method': 'POST'}
        )
        result = await proc.prepare_transaction()
        assert result['method'] == 'POST'
        assert result['form_data'] is not None

    @pytest.mark.asyncio
    async def test_rest_method_default(self):
        payment = MockPayment(backend='dummy')
        proc = DummyProcessor(payment)
        result = await proc.prepare_transaction()
        assert result['method'] == 'REST'


class TestDummyHandleCallback:
    @pytest.mark.asyncio
    async def test_confirm_payment(self):
        payment = MockPayment(
            backend='dummy', status=PaymentStatus.PREPARED
        )
        create_payment_machine(payment)
        proc = DummyProcessor(payment)
        await proc.handle_callback(
            data={'new_status': 'confirm_payment'},
            headers={},
        )
        assert payment.status == PaymentStatus.PARTIAL

    @pytest.mark.asyncio
    async def test_fail(self):
        payment = MockPayment(
            backend='dummy', status=PaymentStatus.NEW
        )
        create_payment_machine(payment)
        proc = DummyProcessor(payment)
        await proc.handle_callback(
            data={'new_status': 'fail'}, headers={}
        )
        assert payment.status == PaymentStatus.FAILED


class TestDummyFetchPaymentStatus:
    @pytest.mark.asyncio
    async def test_returns_status(self):
        payment = MockPayment(backend='dummy')
        proc = DummyProcessor(
            payment,
            config={'confirmation_status': 'confirm_payment'},
        )
        result = await proc.fetch_payment_status()
        assert result['status'] == 'confirm_payment'


class TestDummyCharge:
    @pytest.mark.asyncio
    async def test_charge_full(self):
        payment = MockPayment(
            backend='dummy',
            amount_required=Decimal('100'),
        )
        proc = DummyProcessor(payment)
        result = await proc.charge()
        assert result['success'] is True
        assert result['amount_charged'] == Decimal('100')

    @pytest.mark.asyncio
    async def test_charge_partial(self):
        payment = MockPayment(
            backend='dummy',
            amount_required=Decimal('100'),
        )
        proc = DummyProcessor(payment)
        result = await proc.charge(amount=Decimal('50'))
        assert result['amount_charged'] == Decimal('50')


class TestDummyReleaseLock:
    @pytest.mark.asyncio
    async def test_returns_locked_amount(self):
        payment = MockPayment(
            backend='dummy',
            amount_locked=Decimal('100'),
        )
        proc = DummyProcessor(payment)
        amount = await proc.release_lock()
        assert amount == Decimal('100')


class TestDummyStartRefund:
    @pytest.mark.asyncio
    async def test_refund_full(self):
        payment = MockPayment(
            backend='dummy',
            amount_paid=Decimal('100'),
        )
        proc = DummyProcessor(payment)
        amount = await proc.start_refund()
        assert amount == Decimal('100')

    @pytest.mark.asyncio
    async def test_refund_partial(self):
        payment = MockPayment(
            backend='dummy',
            amount_paid=Decimal('100'),
        )
        proc = DummyProcessor(payment)
        amount = await proc.start_refund(amount=Decimal('30'))
        assert amount == Decimal('30')


class TestDummyCancelRefund:
    @pytest.mark.asyncio
    async def test_returns_true(self):
        payment = MockPayment(backend='dummy')
        proc = DummyProcessor(payment)
        result = await proc.cancel_refund()
        assert result is True
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_dummy_backend.py -v
```

Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `src/getpaid_core/backends/__init__.py`:

```python
"""Built-in payment backends."""
```

Create `src/getpaid_core/backends/dummy.py`:

```python
"""Dummy payment backend for development and testing.

Makes zero HTTP calls. Serves as a reference implementation
for backend authors.
"""

from decimal import Decimal

from getpaid_core.fsm import ALLOWED_CALLBACKS
from getpaid_core.processor import BaseProcessor
from getpaid_core.types import ChargeResponse
from getpaid_core.types import PaymentStatusResponse
from getpaid_core.types import TransactionResult


class DummyProcessor(BaseProcessor):
    """Dummy processor that simulates all payment operations."""

    slug = 'dummy'
    display_name = 'Dummy'
    accepted_currencies = [
        'PLN', 'EUR', 'USD', 'GBP', 'CHF', 'CZK',
    ]

    async def prepare_transaction(
        self, **kwargs
    ) -> TransactionResult:
        method = self.get_setting('method', 'REST')
        if method == 'POST':
            return TransactionResult(
                redirect_url='https://dummy.example.com/form',
                form_data={
                    'payment_id': self.payment.id,
                    'amount': str(self.payment.amount_required),
                    'currency': self.payment.currency,
                },
                method='POST',
                headers={},
            )
        elif method == 'GET':
            return TransactionResult(
                redirect_url=(
                    f'https://dummy.example.com/pay'
                    f'/{self.payment.id}'
                ),
                form_data=None,
                method='GET',
                headers={},
            )
        else:
            return TransactionResult(
                redirect_url=(
                    f'https://dummy.example.com/pay'
                    f'/{self.payment.id}'
                ),
                form_data=None,
                method='REST',
                headers={},
            )

    async def handle_callback(
        self, data: dict, headers: dict, **kwargs
    ) -> None:
        new_status = data.get('new_status')
        if (
            new_status
            and new_status in ALLOWED_CALLBACKS
        ):
            trigger = getattr(self.payment, new_status, None)
            if trigger and callable(trigger):
                trigger()

    async def fetch_payment_status(
        self, **kwargs
    ) -> PaymentStatusResponse:
        status = self.get_setting(
            'confirmation_status', 'confirm_payment'
        )
        return PaymentStatusResponse(status=status)

    async def charge(
        self, amount: Decimal | None = None, **kwargs
    ) -> ChargeResponse:
        charged = (
            amount
            if amount is not None
            else self.payment.amount_required
        )
        return ChargeResponse(
            amount_charged=charged,
            success=True,
            async_call=False,
        )

    async def release_lock(self, **kwargs) -> Decimal:
        return self.payment.amount_locked

    async def start_refund(
        self, amount: Decimal | None = None, **kwargs
    ) -> Decimal:
        return (
            amount
            if amount is not None
            else self.payment.amount_paid
        )

    async def cancel_refund(self, **kwargs) -> bool:
        return True
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_dummy_backend.py -v
```

Expected: all PASS

**Step 5: Commit**

```bash
git add src/getpaid_core/backends/ tests/test_dummy_backend.py
git commit -m "feat: add dummy backend reference implementation"
```

---

## Phase 8: Integration Test & Final Verification

### Task 14: End-to-End Integration Test

**Files:**
- Create: `tests/test_integration.py`

This test runs a complete payment lifecycle using the dummy backend, mock
repository, and PaymentFlow -- verifying all components work together.

**Step 1: Write the integration test**

Create `tests/test_integration.py`:

```python
"""End-to-end integration tests for getpaid-core.

Tests full payment lifecycle: create -> prepare -> callback -> paid.
Uses the dummy backend and in-memory repository.
"""

from decimal import Decimal
from unittest.mock import patch

import pytest

from getpaid_core.backends.dummy import DummyProcessor
from getpaid_core.enums import PaymentStatus
from getpaid_core.flow import PaymentFlow
from getpaid_core.registry import PluginRegistry
from tests.conftest import MockOrder
from tests.conftest import MockRepository


@pytest.fixture
def e2e_registry():
    reg = PluginRegistry()
    reg._discovered = True
    reg.register(DummyProcessor)
    return reg


@pytest.fixture
def e2e_flow(e2e_registry):
    repo = MockRepository()
    with patch('getpaid_core.flow.registry', e2e_registry):
        yield PaymentFlow(
            repository=repo,
            config={'dummy': {'method': 'REST'}},
        )


class TestFullPaymentLifecycle:
    @pytest.mark.asyncio
    async def test_create_prepare_callback_paid(
        self, e2e_flow
    ):
        """Full happy path: create -> prepare -> callback -> paid."""
        order = MockOrder(
            total=Decimal('250.00'), currency='PLN'
        )

        # 1. Create payment
        payment = await e2e_flow.create_payment(
            order, 'dummy'
        )
        assert payment.status == PaymentStatus.NEW
        assert payment.amount_required == Decimal('250.00')

        # 2. Prepare (transitions to PREPARED)
        result = await e2e_flow.prepare(payment)
        assert payment.status == PaymentStatus.PREPARED
        assert result['method'] == 'REST'

        # 3. Callback: confirm_payment (-> PARTIAL)
        await e2e_flow.handle_callback(
            payment,
            data={'new_status': 'confirm_payment'},
            headers={},
        )
        assert payment.status == PaymentStatus.PARTIAL

        # 4. Set amount_paid and mark as paid
        payment.amount_paid = Decimal('250.00')
        await e2e_flow.handle_callback(
            payment,
            data={'new_status': 'mark_as_paid'},
            headers={},
        )
        assert payment.status == PaymentStatus.PAID


class TestRefundLifecycle:
    @pytest.mark.asyncio
    async def test_paid_refund_cycle(self, e2e_flow):
        """Paid -> start_refund -> confirm_refund -> refunded."""
        order = MockOrder(total=Decimal('100.00'))
        payment = await e2e_flow.create_payment(
            order, 'dummy'
        )

        # Get to PAID state
        await e2e_flow.prepare(payment)
        await e2e_flow.handle_callback(
            payment,
            data={'new_status': 'confirm_payment'},
            headers={},
        )
        payment.amount_paid = Decimal('100.00')
        await e2e_flow.handle_callback(
            payment,
            data={'new_status': 'mark_as_paid'},
            headers={},
        )
        assert payment.status == PaymentStatus.PAID

        # Start refund
        refund_amount = await e2e_flow.start_refund(payment)
        assert refund_amount == Decimal('100.00')
        assert payment.status == PaymentStatus.REFUND_STARTED

        # Confirm refund (back to PARTIAL)
        await e2e_flow.handle_callback(
            payment,
            data={'new_status': 'confirm_refund'},
            headers={},
        )
        assert payment.status == PaymentStatus.PARTIAL

        # Mark as fully refunded
        payment.amount_refunded = Decimal('100.00')
        await e2e_flow.handle_callback(
            payment,
            data={'new_status': 'mark_as_refunded'},
            headers={},
        )
        assert payment.status == PaymentStatus.REFUNDED


class TestPreAuthLifecycle:
    @pytest.mark.asyncio
    async def test_preauth_charge_pay(self, e2e_flow):
        """NEW -> PREPARED -> PRE_AUTH -> IN_CHARGE -> PARTIAL -> PAID."""
        order = MockOrder(total=Decimal('200.00'))
        payment = await e2e_flow.create_payment(
            order, 'dummy'
        )

        await e2e_flow.prepare(payment)
        assert payment.status == PaymentStatus.PREPARED

        # Lock (pre-auth)
        await e2e_flow.handle_callback(
            payment,
            data={'new_status': 'confirm_lock'},
            headers={},
        )
        assert payment.status == PaymentStatus.PRE_AUTH

        # Charge
        result = await e2e_flow.charge(payment)
        assert result['success'] is True
        assert payment.status == PaymentStatus.IN_CHARGE

        # Payment received
        await e2e_flow.handle_callback(
            payment,
            data={'new_status': 'confirm_payment'},
            headers={},
        )
        assert payment.status == PaymentStatus.PARTIAL

        payment.amount_paid = Decimal('200.00')
        await e2e_flow.handle_callback(
            payment,
            data={'new_status': 'mark_as_paid'},
            headers={},
        )
        assert payment.status == PaymentStatus.PAID


class TestFailureLifecycle:
    @pytest.mark.asyncio
    async def test_prepared_to_failed(self, e2e_flow):
        order = MockOrder()
        payment = await e2e_flow.create_payment(
            order, 'dummy'
        )
        await e2e_flow.prepare(payment)
        assert payment.status == PaymentStatus.PREPARED

        await e2e_flow.handle_callback(
            payment,
            data={'new_status': 'fail'},
            headers={},
        )
        assert payment.status == PaymentStatus.FAILED


class TestPullFlow:
    @pytest.mark.asyncio
    async def test_fetch_and_update(self, e2e_flow):
        order = MockOrder()
        payment = await e2e_flow.create_payment(
            order, 'dummy'
        )
        await e2e_flow.prepare(payment)

        payment = await e2e_flow.fetch_and_update_status(
            payment
        )
        # Default confirmation_status is confirm_payment
        assert payment.status == PaymentStatus.PARTIAL
```

**Step 2: Run the integration test**

```bash
uv run pytest tests/test_integration.py -v
```

Expected: all PASS

**Step 3: Run full suite**

```bash
uv run pytest --cov=getpaid_core --cov-report=term-missing -v
```

Expected: all PASS with good coverage

**Step 4: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add end-to-end integration tests"
```

---

### Task 15: Final Lint, Format, and Tag

**Step 1: Lint and format**

```bash
uv run ruff check src/ tests/ --fix
uv run ruff format src/ tests/
```

**Step 2: Run full suite one more time**

```bash
uv run pytest --cov=getpaid_core --cov-report=term-missing -v
```

**Step 3: Commit any remaining fixes**

```bash
git add -A
git commit -m "style: final lint and format pass"
```

**Step 4: Tag the release**

```bash
git tag v0.1.0
```

---

## Summary

| Phase | Tasks | What's built |
|-------|-------|-------------|
| 0 | 1 | Project setup (pyproject.toml, tooling) |
| 1 | 2-4 | Enums, types, exceptions |
| 2 | 5-6 | Protocols, BaseProcessor ABC |
| 3 | 7 | FSM with transitions library |
| 4 | 8 | Plugin registry |
| 5 | 9-10 | Validators, PaymentFlow orchestrator |
| 6 | 11-12 | Public API, lint pass |
| 7 | 13 | Dummy backend reference implementation |
| 8 | 14-15 | Integration tests, final verification |

**Total: 15 tasks, ~8 source modules, ~8 test modules**

After this plan is complete, `getpaid-core` v0.1.0 is a fully functional,
well-tested, framework-agnostic payment processing library ready for:

1. Framework adapter development (django-getpaid v3, litestar-getpaid, fastapi-getpaid)
2. Plugin conversion (django-getpaid-payu -> getpaid-payu, etc.)
3. Cookiecutter template rewrite
