# Getting Started

## Installation

Install getpaid-core from PyPI (distributed as `python-getpaid-core`):

```bash
pip install python-getpaid-core
```

Or add it as a dependency with uv:

```bash
uv add python-getpaid-core
```

## Basic Concepts

getpaid-core is a **library, not a framework**. It provides building blocks
that framework adapters (like django-getpaid) use to implement payment
processing. You typically don't use getpaid-core directly in application code
— instead, you use a framework adapter.

If you're building a **new framework adapter** or a **payment gateway plugin**,
read on.

## Creating a Payment Processor Plugin

Every payment gateway needs a processor — a class that knows how to talk to
that gateway's API. Subclass `BaseProcessor` and implement at least
`prepare_transaction`:

```python
from decimal import Decimal
from getpaid_core.processor import BaseProcessor
from getpaid_core.types import TransactionResult


class MyGatewayProcessor(BaseProcessor):
    slug = "my-gateway"
    display_name = "My Payment Gateway"
    accepted_currencies = ["USD", "EUR", "PLN"]
    sandbox_url = "https://sandbox.mygateway.com"
    production_url = "https://api.mygateway.com"

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        # Call your gateway's API to create a payment session
        api_key = self.get_setting("api_key")
        amount = self.payment.amount_required
        currency = self.payment.currency

        # ... call gateway API ...

        return TransactionResult(
            redirect_url="https://mygateway.com/pay/session123",
            form_data=None,
            method="GET",
            headers={},
        )
```

### Callback verification is mandatory

If your processor handles PUSH callbacks, it **must** implement
`verify_callback` to authenticate them (signature/HMAC checks, shared
secrets, etc.) and raise `InvalidCallbackError` when verification fails.
The default implementation fails closed by raising `NotImplementedError`
so an unverified callback can never be processed by accident:

```python
from getpaid_core.exceptions import InvalidCallbackError


class MyGatewayProcessor(BaseProcessor):
    ...

    async def verify_callback(self, data: dict, headers: dict, **kwargs) -> None:
        if not self._signature_is_valid(data, headers):
            raise InvalidCallbackError("Invalid callback signature")
```

If the provider genuinely offers no verification mechanism, override the
method explicitly with a documented no-op.

## Registering a Plugin

Plugins are discovered via Python entry points. Add this to your plugin's
`pyproject.toml`:

```toml
[project.entry-points."getpaid.backends"]
my-gateway = "my_gateway.processor:MyGatewayProcessor"
```

Or register manually for testing:

```python
from getpaid_core.registry import registry

registry.register(MyGatewayProcessor)
```

## Payment Updates

Payments move through states by applying semantic `PaymentUpdate` objects.
Processors return updates from callbacks and status polling, and the flow
applies them to the payment object:

```python
from decimal import Decimal

from getpaid_core.fsm import apply_payment_update
from getpaid_core.types import PaymentUpdate

apply_payment_update(
    payment,
    PaymentUpdate(payment_event="prepared"),
)

apply_payment_update(
    payment,
    PaymentUpdate(
        payment_event="payment_captured",
        paid_amount=Decimal("100.00"),
    ),
)
```

See {doc}`concepts` for the lifecycle rules and semantic event mapping.
