# python-getpaid-core

[![PyPI version](https://img.shields.io/pypi/v/python-getpaid-core)](https://pypi.org/project/python-getpaid-core/)
[![Python version](https://img.shields.io/pypi/pyversions/python-getpaid-core)](https://pypi.org/project/python-getpaid-core/)
[![License](https://img.shields.io/pypi/l/python-getpaid-core)](https://github.com/django-getpaid/python-getpaid-core/blob/main/LICENSE)

**Framework-agnostic payment processing core.**

`python-getpaid-core` is the foundation of the Getpaid ecosystem. It provides the abstract interfaces, finite state machines (FSM), and plugin registry needed to build a robust payment system without coupling your logic to a specific web framework or payment provider.

## Installation

```bash
pip install python-getpaid-core
```

## Quick Start: Creating a Custom Processor

To implement a new payment backend, subclass `BaseProcessor` and implement at least `prepare_transaction`.

```python
from getpaid_core import BaseProcessor
from getpaid_core.types import TransactionResult

class MyPaymentProcessor(BaseProcessor):
    slug = "my-provider"
    display_name = "My Payment Provider"
    accepted_currencies = ["USD", "EUR"]

    async def prepare_transaction(self, **kwargs) -> TransactionResult:
        # Generate payment link or form data
        return TransactionResult(
            redirect_url=f"https://api.provider.com/pay/{self.payment.id}",
            method="GET"
        )
```

### Registering your Processor

Register your processor using entry points in your `pyproject.toml` so it can be discovered by the registry:

```toml
[project.entry-points."getpaid.backends"]
my-provider = "my_package.processors:MyPaymentProcessor"
```

## Architecture Overview

- **BaseProcessor**: The abstract base class that all payment gateway plugins must implement. It provides the standard interface for transaction preparation, callback handling, charging, and refunds.
- **PaymentFlow**: Manages the payment lifecycle using a Finite State Machine (FSM) powered by the `transitions` library. It ensures that payments move between states (e.g., `NEW` -> `PREPARED` -> `PAID`) according to strict business rules.
- **PluginRegistry**: A central service for discovering and managing payment processors registered via `getpaid.backends` entry points.
- **State Machine (FSM)**: Dynamically attaches state-machine triggers to payment objects at runtime, allowing for clean and predictable state transitions.

## API Summary

| Class / Module | Role |
| --- | --- |
| `BaseProcessor` | Abstract base for implementing payment gateways. |
| `PaymentFlow` | FSM logic for managing payment lifecycles. |
| `PaymentStatus` | Enum for all possible payment states (NEW, PAID, FAILED, etc.). |
| `registry` | Singleton registry for backend discovery. |
| `TransactionResult` | Standard response for transaction initiation. |
| `GetPaidException` | Base exception for all payment-related errors. |

## Ecosystem

`getpaid-core` is the heart of a larger ecosystem designed to make payment processing easy in any Python web application.

### Framework Wrappers
- [django-getpaid](https://github.com/django-getpaid/django-getpaid) — Official Django integration.
- [litestar-getpaid](https://github.com/django-getpaid/litestar-getpaid) — Official Litestar integration.
- [fastapi-getpaid](https://github.com/django-getpaid/fastapi-getpaid) — Official FastAPI integration.

### Processor Plugins
- [python-getpaid-payu](https://github.com/django-getpaid/python-getpaid-payu) — PayU backend.
- [python-getpaid-paynow](https://github.com/django-getpaid/python-getpaid-paynow) — Paynow backend.
- [python-getpaid-bitpay](https://github.com/django-getpaid/python-getpaid-bitpay) — BitPay backend.
- [python-getpaid-przelewy24](https://github.com/django-getpaid/python-getpaid-przelewy24) — Przelewy24 backend.

## License

This project is licensed under the MIT License.
