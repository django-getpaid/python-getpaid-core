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
| `REFUNDED` | `"refunded"` | Fully refunded (money moved back to the buyer) |
| `CANCELLED` | `"cancelled"` | Pre-auth lock released with nothing captured |

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
lock_released    -> CANCELLED (nothing paid) or REFUNDED (partially captured)
```

### Transition Rules

The state engine raises `InvalidTransitionError` when an event is incompatible
with the current payment state. This applies to *every* event, including
`prepared` and `locked` — there are no silently ignored events.

- You cannot capture a payment after it is already refunded.
- You cannot start a refund before the payment has been paid.
- Refund confirmation moves to `REFUNDED` only when `amount_refunded >= amount_paid`.
- Releasing a pre-auth lock with nothing captured marks the payment
  `CANCELLED`; if some amount was already captured, it becomes `REFUNDED`.

### Amount Handling

All financial values must be finite `Decimal` instances; core does not coerce
strings, floats, or integers. Stored balances must satisfy
`0 <= amount_refunded <= amount_paid <= amount_required` and
`0 <= amount_locked <= amount_required - amount_paid`.

#### Requests and results

After application operation validators run, `PaymentFlow` validates charge,
refund, and lock-release amounts before constructing or calling the processor.
Existing status restrictions still apply.

| Operation | Effective request | Supported result amount |
|-----------|-------------------|-------------------------|
| `charge` | Positive, at most `min(amount_locked, amount_required - amount_paid)` | Successful synchronous capture: positive, at most the request; async acceptance: zero through the request; decline: exactly zero |
| `start_refund` | Positive, at most `amount_paid - amount_refunded` | Positive, at most the request; acceptance is not settlement |
| `release_lock` | The entire positive `amount_locked` | Exactly the authorization being released; partial release is not supported |

For both charge and refund, `amount=None` (including an amount removed or set to
`None` by a validator) means the **remaining available balance**, not the original
total. Core passes that explicit `Decimal` to the processor. Exhausted balances
and zero requests are rejected with `InvalidTransitionError`, without a provider
call or repository save.

Invalid returned amounts raise `ReconciliationRequiredError` before changing
local state. Its `context` contains `payment_id`, `operation`, and
`provider_result`; charge errors also retain `charge_result`. A provider call has
already happened: do not blindly retry. Inspect provider state and reconcile;
local rejection cannot undo a remote action. Result evidence is untrusted
provider data and must not be logged or exposed indiscriminately.

#### Incoming snapshots

- `locked_amount` is an explicit, positive remaining authorization, bounded by
  `amount_required - amount_paid`. `LOCKED` cannot create `PRE_AUTH` with zero or
  absent authorization.
- `paid_amount` and `refunded_amount` are explicit **cumulative** totals for their
  respective events. Zero is a valid observation, not a new money-moving request.
  Captured totals cannot exceed `amount_required`; refunded totals cannot exceed
  `amount_paid`. Captured increments reduce `amount_locked`.
- Within permitted lifecycle transitions, a lower valid cumulative snapshot
  preserves the larger recorded total. Negative and non-finite values are invalid,
  not stale observations. This does not change capture/refund lifecycle ordering
  rules or permit new transitions after refunds.
- Every supplied financial field is validated, including fields on metadata-only
  updates. Invalid updates roll back amounts, status, external ID, fraud state,
  metadata, and the provider event ID. Already-applied event IDs remain no-op
  replays, even if the available authorization has since changed.
- `provider_data` stores provider-specific metadata such as refund IDs and applied
  callback IDs.

These checks protect the current payment snapshot; they do not reserve pending
amounts, serialize concurrent calls, or provide an operation-idempotency contract.

**Compatibility:** plugins that previously relied on receiving `None` from the
flow now receive a concrete remaining amount. Previously accepted invalid money
is rejected; zero remains supported only for cumulative observations, pending
charge results, and declined charge results as specified above.

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

This is the released 3.x protocol: it takes a payment object the caller
loaded earlier and saves it. Two independent snapshots of one payment can
therefore overwrite each other's committed amounts.

### DurablePaymentRepository Protocol

```python
class DurablePaymentRepository(Protocol):
    async def get_payment_facts(self, payment_id: str) -> PaymentFacts: ...
    async def reserve_operation(self, payment_id: str, intent: OperationIntent) -> OperationRecord: ...
    async def apply_observation(self, payment_id: str, update: PaymentUpdate | None) -> ObservationPlan: ...
    async def record_operation_outcome(
        self, payment_id: str, operation_id: str, outcome: OperationOutcome
    ) -> OutcomePlan: ...
    async def get_operation(self, payment_id: str, operation_id: str) -> OperationRecord | None: ...
    async def list_unresolved_operations(self) -> Sequence[OperationRecord]: ...
```

Every operation addresses a payment by identity, applies core's rules to
the payment's *current* stored state, and returns committed state. When a
repository provides all of them, `PaymentFlow` routes callbacks and
polling through it instead of saving the caller's object. See
[Durable Storage Contract](durable-storage.md) for the mandatory and
optional capabilities and the upgrade boundary.

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
├── InvalidTransitionError
├── ReconciliationRequiredError
├── UnsupportedRepositoryError
├── StateConflictError
├── OperationConflictError
├── ConformanceError
└── BackendNotFoundError (also a KeyError)
```

`BackendNotFoundError` is raised by `registry.get_by_slug()` for unknown
slugs; it also subclasses `KeyError` so legacy `except KeyError` code keeps
working. `ReconciliationRequiredError` is raised by `PaymentFlow.charge()`
when the gateway charge succeeded but recording it locally failed — it
carries the gateway result in its `charge_result` attribute so operators
can reconcile the payment manually. That result may contain sensitive
provider metadata — see [Logging and Diagnostics](#logging-and-diagnostics).

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

## Logging and Diagnostics

Core logs charge outcomes through the `getpaid_core.flow` logger: a
WARNING when the gateway declines a charge, and a CRITICAL when the
gateway charge succeeded but recording it locally failed. Into both
records core interpolates one allowlisted summary of safe, core-owned
fields, and nothing else of its own:

| Field | Source |
|-------|--------|
| `payment_id` | `Payment.id` |
| `operation` | the flow operation (`"charge"`) |
| `backend` | `Payment.backend` |
| `external_id` | `Payment.external_id`, the provider correlation handle |
| `currency` | `Payment.currency` |
| `success`, `async_call` | `ChargeResult` outcome flags |
| `amount_charged` | `ChargeResult.amount_charged` |
| `amount_required` | `Payment.amount_required` |
| `provider_data_entries` | how many `provider_data` entries the result holds — no keys, no values |

The CRITICAL record additionally carries the local failure's traceback
(`exc_info`), which comes from the repository or FSM code that raised —
not from the provider result. A repository whose exception messages
embed payment data therefore still reaches the log sink through that
traceback; that payload is the adapter's to control, not core's.

`provider_data` is plugin-defined `dict[str, Any]`. It may hold stored
credentials, raw provider responses or buyer details, so core never
interpolates its keys or values into a log record; failure paths are
exactly where such payloads are most likely to be present. Core has no
typed provider error field either, so it logs no provider error code — a
backend that knows its provider's response schema can log its own
allowlist.

Full recovery evidence is preserved on the raised
`ReconciliationRequiredError`: `charge_result` (the same object is also
under `context["charge_result"]`) carries the untouched `ChargeResult`,
`provider_data` included. Treat it as sensitive — route it to a
controlled channel such as an operator-only reconciliation record, and do
not hand it to a general-purpose logger or an error-reporting service
without redacting it first.
