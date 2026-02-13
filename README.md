# getpaid-core

[![PyPI](https://img.shields.io/pypi/v/getpaid-core.svg)](https://pypi.org/project/getpaid-core/)
[![Python Version](https://img.shields.io/pypi/pyversions/getpaid-core)](https://pypi.org/project/getpaid-core/)
[![License](https://img.shields.io/pypi/l/getpaid-core)](https://github.com/django-getpaid/getpaid-core/blob/main/LICENSE)

Framework-agnostic payment processing library for Python. Provides the core
abstractions — enums, protocols, FSM, processor base class, plugin registry,
and exception hierarchy — that framework-specific adapters build on.

## Architecture

getpaid-core defines the **what** of payment processing without coupling to
any web framework:

- **Enums** (`PaymentStatus`, `FraudStatus`, `BackendMethod`, `ConfirmationMethod`)
  define all valid states and methods.
- **Protocols** (`Payment`, `Order`, `PaymentRepository`) define structural
  contracts that framework models must satisfy.
- **FSM** (`create_payment_machine`, `create_fraud_machine`) attaches
  state-machine triggers to payment objects at runtime using the `transitions`
  library.
- **BaseProcessor** is an abstract class that payment gateway plugins subclass
  to implement `prepare_transaction`, `handle_callback`, `charge`, etc.
- **PluginRegistry** discovers and stores payment backend processors via
  entry points or manual registration.
- **Exceptions** provide a structured hierarchy for payment errors.

## Framework Adapters

- **[django-getpaid](https://github.com/django-getpaid/django-getpaid)** —
  Django adapter (models, views, forms, admin)

## Installation

```bash
pip install getpaid-core
```

You typically install this as a dependency of a framework adapter rather than
directly.

## Quick Example

```python
from getpaid_core.enums import PaymentStatus
from getpaid_core.fsm import create_payment_machine

# Any object satisfying the Payment protocol works
payment = MyPayment(status=PaymentStatus.NEW, amount_required=100)
machine = create_payment_machine(payment)

# FSM trigger methods are attached directly to the object
payment.confirm_prepared()
assert payment.status == PaymentStatus.PREPARED
```

## Requirements

- Python 3.12+
- transitions
- httpx
- anyio

## License

MIT

## Credits

Created by [Dominik Kozaczko](https://github.com/dekoza).
