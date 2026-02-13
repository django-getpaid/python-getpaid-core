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

## Payment State Machine

Payments move through states via an FSM powered by the `transitions` library.
The machine attaches trigger methods directly to payment objects:

```python
from getpaid_core.fsm import create_payment_machine

machine = create_payment_machine(payment)

# Now payment has trigger methods:
payment.confirm_prepared()   # NEW -> PREPARED
payment.confirm_lock(amount=100)  # PREPARED -> PRE_AUTH
payment.confirm_payment(amount=100)  # PRE_AUTH -> PARTIAL
payment.mark_as_paid()       # PARTIAL -> PAID (if fully paid)
```

See {doc}`concepts` for the full state diagram and transition rules.
