# Core Concepts

## Payment Statuses

Payments move through these states:

| Status | Value | Description |
|--------|-------|-------------|
| `NEW` | `"new"` | Just created, not yet sent to gateway |
| `PREPARED` | `"prepared"` | Sent to gateway, waiting for buyer action |
| `PRE_AUTH` | `"pre-auth"` | Amount pre-authorized (locked) |
| `IN_CHARGE` | `"charge_started"` | Charge request sent for pre-authed payment |
| `PARTIAL` | `"partially_paid"` | Some amount received |
| `PAID` | `"paid"` | Fully paid |
| `FAILED` | `"failed"` | Payment failed |
| `REFUND_STARTED` | `"refund_started"` | Refund initiated |
| `REFUNDED` | `"refunded"` | Fully refunded or lock released |

## Payment Events

```
prepared         -> PREPARED
locked           -> PRE_AUTH
charge_requested -> IN_CHARGE
payment_captured -> PARTIAL or PAID
failed           -> FAILED
refund_requested -> REFUND_STARTED
refund_confirmed -> PARTIAL or REFUNDED
refund_cancelled -> active paid status
lock_released    -> REFUNDED
```

### Transition Rules

The state engine raises `InvalidTransitionError` when an event is incompatible
with the current payment state.

- You cannot capture a payment after it is already refunded.
- You cannot start a refund before the payment has been paid.
- Refund confirmation moves to `REFUNDED` only when `amount_refunded >= amount_paid`.

### Amount Handling

- `locked_amount` stores a pre-authorized amount.
- `paid_amount` updates captured funds and may reduce `amount_locked`.
- `refunded_amount` tracks refund progress.
- `provider_data` stores provider-specific metadata such as refund IDs and applied callback IDs.

## Fraud Statuses

| Status | Value | Description |
|--------|-------|-------------|
| `UNKNOWN` | `"unknown"` | Not yet checked |
| `ACCEPTED` | `"accepted"` | Passed fraud check |
| `REJECTED` | `"rejected"` | Failed fraud check |
| `CHECK` | `"check"` | Needs manual review |

### Fraud Transitions

```
UNKNOWN ── flag_as_fraud ──► REJECTED
UNKNOWN ── flag_as_legit ──► ACCEPTED
UNKNOWN ── flag_for_check ─► CHECK
CHECK ──── mark_as_fraud ──► REJECTED
CHECK ──── mark_as_legit ──► ACCEPTED
```

## Protocols

getpaid-core uses Python protocols (structural subtyping) instead of base
classes for framework integration. Any object with the right attributes and
methods satisfies the protocol — no inheritance required.

### Order Protocol

```python
class Order(Protocol):
    def get_total_amount(self) -> Decimal: ...
    def get_buyer_info(self) -> BuyerInfo: ...
    def get_description(self) -> str: ...
    def get_currency(self) -> str: ...
    def get_items(self) -> list[ItemInfo]: ...
    def get_return_url(self, success: bool | None = None) -> str: ...
```

### Payment Protocol

```python
class Payment(Protocol):
    id: str
    order: Order
    amount_required: Decimal
    currency: str
    status: str
    backend: str
    external_id: str | None
    description: str | None
    amount_paid: Decimal
    amount_locked: Decimal
    amount_refunded: Decimal
    fraud_status: str
    fraud_message: str
    provider_data: dict[str, Any]
```

### PaymentRepository Protocol

```python
class PaymentRepository(Protocol):
    async def get_by_id(self, payment_id: str) -> Payment: ...
    async def create(self, **kwargs) -> Payment: ...
    async def save(self, payment: Payment) -> Payment: ...
    async def update_status(self, payment_id: str, status: str, **fields) -> Payment: ...
    async def list_by_order(self, order_id: str) -> list[Payment]: ...
```

## Plugin Registry

The `PluginRegistry` discovers payment backend processors via Python entry
points (the `getpaid.backends` group) and provides lookup by slug or currency:

```python
from getpaid_core.registry import registry

# Auto-discovers on first use
backends = registry.get_for_currency("PLN")
choices = registry.get_choices("EUR")  # [(slug, display_name), ...]
processor_class = registry.get_by_slug("my-gateway")
```

## Exception Hierarchy

```
GetPaidException
├── CommunicationError
│   ├── ChargeFailure
│   ├── LockFailure
│   └── RefundFailure
├── CredentialsError
├── InvalidCallbackError
└── InvalidTransitionError
```

All exceptions accept an optional `context` dict for structured error info:

```python
raise ChargeFailure("Gateway returned 500", context={"status_code": 500})
```

## Type Definitions

| Type | Description |
|------|-------------|
| `BuyerInfo` | TypedDict with `email`, `first_name`, `last_name`, `phone` (all optional) |
| `ItemInfo` | TypedDict with `name`, `quantity`, `unit_price` |
| `ChargeResult` | Dataclass with `amount_charged`, `success`, `async_call`, `provider_data` |
| `PaymentUpdate` | Dataclass describing semantic payment/fraud events and amounts |
| `RefundResult` | Dataclass with refund amount and provider metadata |
| `TransactionResult` | Dataclass with redirect, method, external ID, and provider metadata |
