# Core Concepts

## Durable Operation Intents

`DurablePaymentFlow.execute_operation()` requires an application-assigned
operation ID for each deliberate prepare, capture, authorization release,
refund or refund cancellation. Retries use the same ID and immutable request;
separate partial captures/refunds use new IDs. Reservation freezes the concrete
amount and starting totals, then an atomic submission claim precedes provider
I/O. See [Durable Storage Contract](durable-storage.md#durable-operation-dispatch)
for processor capabilities, adapter upgrades and recovery.

Operation state is separate from payment status: `RESERVED`, `SUBMITTING`,
`PROVIDER_PENDING`, `SUCCEEDED`, `REJECTED` and nonterminal `UNKNOWN` describe
one intent. Acceptance is not settlement. Unknown/pending operations block
unrelated commands while observations and reconciliation remain available.
The structured `OperationResult` reports operation identity, outcome, committed
snapshot and reconciliation requirement. Retrying an uncertain command is not
permission to submit it again, even after a crash or lease expiry.

These guarantees belong to the explicit durable flow, **not** the released
`PaymentFlow` API described in the legacy request/result sections below.

## Durable Recovery

Local recording failure is not provider rejection or remote rollback. All five
mutating operations share the same safe recovery boundary. Errors carry normalized
provider evidence, payment/operation identity and the original local cause. A
bounded local retention attempt preserves evidence without inventing settlement;
if storage is unavailable, the earlier durable submission still makes the intent
discoverable after restart. Never use an error as permission to replay the command.

Applications query operations explicitly through `reconcile_operation` and discover
work through repository lookup/list methods; core runs no scheduler. A provider's
`OperationNotFound` remains unknown unless its declared contract excludes execution
conclusively. Pending/unknown are ordinary structured outcomes, not failed payments.

For cases queries cannot settle, `resolve_operation` accepts an `OperatorResolution`
with actor, reason, evidence references and decision time. The integration authorizes
the operator; core atomically compares reviewed snapshots, applies financial rules
and commits audit with settlement. Stale decisions fail, old evidence survives, and
a new contradictory callback can reopen reconciliation. Time alone never resolves
uncertainty. See the [recovery contract](durable-storage.md#audited-operator-resolution)
for storage upgrade and error migration details. Cancellation-aware cleanup remains
separate work; these guarantees do not extend the released `PaymentFlow`.

## Durable Observations and Reconciliation

A cumulative observation reports what has happened, not a new command.
`PaymentObservation` extends `PaymentUpdate` with optional operation evidence:
`operation_id` must come from an authenticated provider echo, or
`outcome.correlation` must uniquely match a retained provider handle. The `outcome`
is an `OperationOutcome`; aggregate totals, equal amounts and the currently active
intent never establish which operation completed.

With `delta_only=True`, delta fields are never added directly to current money.
A correlated outcome establishes cumulative money using the frozen reservation
and complete retained operation history. Otherwise core retains normalized
evidence and requires reconciliation instead of guessing. Independently supplied
captured and refunded totals both apply, regardless of their single event label.

Equal/lower capture observations preserve pending, partial and full refund
progress, even with different or missing event identities. A genuinely increased
capture during or after refund is recorded when within financial bounds,
without reducing refunded funds. It requires reconciliation and blocks new
reservations/submission rights; it never triggers an automatic compensating
refund. Refunding still does not authorize a new capture command.

Cancellation must say what it cancels. A `LOCK_RELEASED` observation with
`cancellation_scope=OperationType.RELEASE_LOCK` releases only remaining
authorization, never captured funds or a pending refund. Ambiguous cancellation
is retained for reconciliation; correlated refund-cancellation outcomes use the
reserved explicit target and preserve racing settlement.

Finite impossible money is retained for investigation, not forced into balances:
`PaymentFacts.observation_conflicts` stores immutable allowlisted semantic JSON,
reason and event identity, without raw metadata. Operation-specific impossible
settlement amounts or cumulative bounds are retained in
`OperationRecord.conflicting_outcomes`, preserving established money and the prior
operation state. Malformed types, nonfinite values and truly impossible lifecycle
transitions still raise atomically. Compact evidence has no automatic expiry.

Adapters must supply complete retained history to `plan_observation` and commit
all `ObservationPlan.operations` atomically with facts and replay evidence,
including retained conflicts. Existing pre-release durable digests need a
[coordinated offline upgrade](durable-storage.md#upgrading-pre-release-durable-records)
from original normalized evidence, or mutation-blocking/reconciliation when it is
unavailable; never discard trusted history blindly. This is separate from legacy
3.x migration and does not claim real-provider or full-ADR assurance.

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
| `REFUND_STARTED` | `"refund_started"` | Refund initiated, not yet resolved |
| `PARTIALLY_REFUNDED` | `"partially_refunded"` | Some captured funds returned |
| `REFUNDED` | `"refunded"` | Every captured fund returned to the buyer |
| `CANCELLED` | `"cancelled"` | Authorization released with nothing captured |

## Payment Events

```
prepared         -> PREPARED
locked           -> PRE_AUTH
charge_requested -> IN_CHARGE (nothing captured yet) or unchanged
payment_captured -> projected status
failed           -> FAILED
refund_requested -> REFUND_STARTED
refund_confirmed -> projected status
refund_cancelled -> projected status
lock_released    -> projected status
```

### Status Projection

Captured funds, refunded funds and the remaining authorization are three
orthogonal facts. The status is a *projection* of them, not a fourth fact, and
`getpaid_core.fsm.project_payment_status` derives it in this precedence:

1. an unresolved refund reports `REFUND_STARTED`, keeping the amounts intact;
2. otherwise refunded funds report `REFUNDED` when they equal the captured
   funds, and `PARTIALLY_REFUNDED` otherwise;
3. otherwise captured funds report `PAID` when they equal `amount_required`,
   and `PARTIAL` (partially *paid*) otherwise;
4. otherwise a positive remaining authorization reports `PRE_AUTH`, and a
   confirmed release of the whole of it reports `CANCELLED`.

Where no settlement rule applies the payment keeps its current status, so
`NEW`, `PREPARED`, `IN_CHARGE` and `FAILED` survive the projection: zero totals
alone are not a cancellation. `amount_locked` and any reconciliation
requirement stay separately visible — one status never describes the whole
financial state.

### Transition Rules

The state engine raises `InvalidTransitionError` when an event is incompatible
with the current payment state. This applies to *every* event, including
`prepared` and `locked` — there are no silently ignored events. Eligibility
follows the payment's current facts rather than the status it happens to hold.

- Capture needs remaining authorization and unpaid required amount. A partial
  capture leaves the remaining authorization usable, so a subsequent capture of
  the rest is a supported command.
- Capture *commands* are refused once any refund is unresolved or any funds
  have been returned: refunding does not reopen capture capacity, and
  collecting replacement funds requires a **new payment**. Incoming capture
  *evidence* is different: equal/lower cumulative totals preserve refund progress,
  including pending and full refunds. Valid increased captures are recorded, not
  refused merely because a refund exists; the durable planner additionally flags
  reconciliation. Recording what happened is not authorizing another capture.
- Refunds need captured funds not yet returned, so a partially refunded payment
  stays refundable down to zero.
- Releasing needs a positive remaining authorization, in any status. It removes
  the whole of it and changes neither captured nor refunded totals, so a
  payment with captured funds stays `PARTIAL` or `PAID` — **not** `REFUNDED`.
  Voiding an uncaptured authorization is not a refund of the captured portion.

### Amount Handling

All financial values must be finite `Decimal` instances; core does not coerce
strings, floats, or integers. Stored balances must satisfy
`0 <= amount_refunded <= amount_paid <= amount_required` and
`0 <= amount_locked <= amount_required - amount_paid`.

#### Legacy requests and results

After application operation validators run, `PaymentFlow` validates charge,
refund, and lock-release amounts before constructing or calling the processor,
together with the fact-based eligibility rules above.

| Operation | Effective request | Supported result amount |
|-----------|-------------------|-------------------------|
| `charge` | Positive, at most `min(amount_locked, amount_required - amount_paid)` | Successful synchronous capture: positive, at most the request; async acceptance: zero through the request; decline: exactly zero |
| `start_refund` | Positive, at most `amount_paid - amount_refunded` | Positive, at most the request; acceptance is not settlement |
| `release_lock` | The entire positive `amount_locked`, whatever the status | Exactly the authorization being released; partial release is not supported |

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

#### Legacy incoming snapshots

The following describes `PaymentFlow` snapshot validation. The durable boundary
instead retains finite impossible money as disputed evidence, as described above.

- `locked_amount` is an explicit, positive remaining authorization, bounded by
  `amount_required - amount_paid`. `LOCKED` cannot create `PRE_AUTH` with zero or
  absent authorization.
- `paid_amount` and `refunded_amount` are explicit **cumulative** totals for their
  respective events. Zero is a valid observation, not a new money-moving request.
  Captured totals cannot exceed `amount_required`; refunded totals cannot exceed
  `amount_paid`. Captured increments reduce `amount_locked`.
- Within permitted lifecycle transitions, a lower valid cumulative snapshot
  preserves the larger recorded total. Negative and non-finite values are invalid,
  not stale observations. This does not change command eligibility: recording
  capture evidence after funds have been returned does not permit a new capture.
- Every supplied financial field is validated, including fields on metadata-only
  updates. Invalid updates roll back amounts, status, external ID, fraud state,
  metadata, and the provider event ID. Already-applied event IDs remain no-op
  replays, even if the available authorization has since changed.
- `provider_data` stores provider-specific metadata such as refund IDs, and — on
  this released path only — the applied callback IDs. Under the durable contract
  replay evidence is core-owned and lives outside `provider_data`; see
  [Durable Storage Contract](durable-storage.md).

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
    async def claim_submission(
        self, payment_id: str, operation_id: str, *, expected_attempt: int,
        now: datetime, retry_until: datetime | None = None,
        idempotency_scope: str | None = None,
    ) -> SubmissionPlan: ...
    async def apply_observation(self, payment_id: str, update: PaymentUpdate | None) -> ObservationPlan: ...
    async def record_operation_outcome(
        self, payment_id: str, operation_id: str, outcome: OperationOutcome
    ) -> OutcomePlan: ...
    async def record_operation_failure(
        self, payment_id: str, operation_id: str, evidence: RecoveryEvidence
    ) -> OperationRecord: ...
    async def resolve_operation(
        self, payment_id: str, operation_id: str, resolution: OperatorResolution,
        *, expected_operation: OperationRecord, expected_facts: PaymentFacts,
    ) -> OutcomePlan: ...
    async def get_operation(self, payment_id: str, operation_id: str) -> OperationRecord | None: ...
    async def list_unresolved_operations(self) -> Sequence[OperationRecord]: ...
    async def list_payments_requiring_reconciliation(self) -> Sequence[PaymentFacts]: ...
```

Every operation addresses a payment by identity, applies core's rules to
the payment's *current* stored state, and returns committed state.
`getpaid_core.durable.DurablePaymentFlow` orchestrates against it and
refuses any repository that does not implement it; `PaymentFlow` keeps
the released behaviour over `PaymentRepository`. See
[Durable Storage Contract](durable-storage.md) for the mandatory and
optional capabilities, replay-evidence ownership, and the upgrade
boundary — including migrating released records and the writer cutover
that must precede the first new-contract write.

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
│   └── OperationEvidenceError
├── ReconciliationRequiredError
├── UnsupportedRepositoryError
├── UnsupportedProcessorError
├── OperationPersistenceError
├── StateConflictError
├── OperationConflictError
│   └── ReconciliationBlockedError
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
| `PaymentObservation` | Durable `PaymentUpdate` subclass with correlated outcome, delta and cancellation scope fields |
| `ObservationConflict` | Immutable retained normalized observation JSON, event identity and reason |
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
