# Changelog

## Unreleased

### Breaking Changes

- **Partial capture no longer strands the remaining authorization**: capture
  and authorization release are now eligible on the payment's current
  financial facts instead of a status guard. After `lock(100)` and
  `capture(30)`, capturing the remaining 70 or releasing it are both
  supported commands; previously both raised `InvalidTransitionError`.
- **Releasing an authorization is not a refund**: `LOCK_RELEASED` on a
  payment with captured funds now projects `PARTIAL` (or `PAID`) and leaves
  `amount_refunded` at zero. This deliberately replaces the v3.2.0 behaviour
  documented below, which reported `REFUNDED` without any money being
  returned. An uncaptured full release still reports `CANCELLED`.
- **New `PaymentStatus.PARTIALLY_REFUNDED`** (`"partially_refunded"`): a
  partly returned payment is now distinguishable from a partly paid one,
  which both previously reported `PARTIAL`. Consumers matching on `PARTIAL`
  after a refund must handle the new member.
- **Refunding does not reopen capture capacity**: capture is refused once a
  refund is unresolved or any funds have been returned, in `PaymentFlow`, in
  the state engine and at durable reservation time. Collecting replacement
  funds requires a new payment.
- **Status is a projection**: `getpaid_core.fsm.project_payment_status`
  derives the public status from captured funds, refunded funds and the
  remaining authorization, with an unresolved refund taking precedence. Zero
  totals alone are not a cancellation, and `NEW`, `PREPARED`, `IN_CHARGE` and
  `FAILED` survive the projection.

- **Replay evidence left `provider_data`**: trusted replay bookkeeping is
  now a core-owned `ReplayRecord` in dedicated storage, committed
  atomically with the financial facts it applies. Provider metadata can no
  longer seed, replace or erase it, so a payload carrying an
  `applied_event_ids` key cannot suppress a genuine capture. Legacy
  lookalike keys survive as ordinary readable metadata and are never
  consulted. `ReplayRecord.for_observation()` now takes the payment's
  `PaymentFacts` rather than a payment id, and `PaymentFacts` gained a
  `backend` field: an event identity is scoped to `(payment, backend,
  identity)`, read from stored facts rather than from the payload.
- **Malformed metadata is refused atomically**: a non-mapping
  `provider_data`, a non-string metadata key or a non-string event
  identity raises `InvalidTransitionError` before anything is planned, so
  committed funds and committed history both survive the rejection.
- **A payment awaiting reconciliation refuses new commands**: reserving an
  operation against facts carrying `reconciliation_required` raises the
  new `ReconciliationBlockedError` (a subclass of
  `OperationConflictError`). Operations already reserved still resume and
  still resolve, and observations are unaffected — callbacks and
  reconciliation continue while commands are blocked.

### Added

- **`getpaid_core.durable.migration`**: a framework-neutral contract for
  reading released 3.x payment records into durable facts.
  `plan_migration(LegacyPaymentState)` preserves legacy amounts, status
  and metadata, produces neither replay evidence nor operation records —
  provider-controlled history is not promoted and operation IDs are not
  invented — and reports `MigrationFinding`s. Ambiguous records and
  records left mid-operation migrate readable but mutation-blocked.
- **Three further adapter conformance checks**: metadata cannot forge or
  erase replay history, malformed metadata is rejected atomically, and a
  payment awaiting reconciliation refuses new commands.

See `docs/adr/0001-durable-money-operations.md`, sections 4, 5 and 6, and
`docs/durable-storage.md` for the migration, cutover and retention rules.

## v3.2.0 (2026-07-04)

### Breaking Changes

- **Fail-closed `verify_callback`**: the default
  `BaseProcessor.verify_callback` no longer silently accepts callbacks —
  it now raises `NotImplementedError`. Every processor must implement
  callback authentication, or override the method explicitly (with a
  documented no-op) if the provider offers no verification. The shipped
  `DummyProcessor` overrides it explicitly as a dev-only no-op.
- **FSM: `PREPARED`/`LOCKED` events in wrong statuses now raise**:
  previously these two events were silently ignored when the payment was
  not in a valid source status, while all other events raised
  `InvalidTransitionError`. They now raise too.
- **FSM: releasing a lock with nothing paid marks the payment
  `CANCELLED`**: `LOCK_RELEASED` on a `PRE_AUTH` payment with
  `amount_paid == 0` now sets the new `PaymentStatus.CANCELLED`
  (`"cancelled"`) instead of `REFUNDED` — no money moved, so nothing was
  refunded. If some amount was already captured, the status remains
  `REFUNDED`.

### Fixed

- `PaymentFlow.charge` no longer loses track of money when the gateway
  charge succeeds but the local update fails: it logs at CRITICAL (with
  payment id and gateway result) and raises the new
  `ReconciliationRequiredError`, which carries the gateway
  `charge_result` for manual reconciliation. A gateway-declined charge
  (`success=False`) is no longer ignored silently — the payment now
  records a `FAILED` event and is persisted.
- Registry hardening: `register`, `unregister` and `discover` now take
  the registry lock; a plugin that fails to import (or does not provide
  a `BaseProcessor` subclass) is skipped with a logged warning instead
  of aborting discovery (or being skipped silently); registering a
  backend with an empty slug raises `ValueError`.
- Provider event dedupe (`applied_event_ids`) lookups are now O(1) via a
  transient set view; the stored list representation in `provider_data`
  is unchanged.
- Removed the stale hardcoded version assertion from the test suite; the
  package version is now only checked dynamically against installed
  metadata.
- Removed the dead `tests.yml` GitHub workflow (cookiecutter residue
  targeting Python 3.7–3.10 via a nonexistent noxfile).

### Added

- `PaymentStatus.CANCELLED` (`"cancelled"`).
- `BackendNotFoundError`, raised by `PluginRegistry.get_by_slug` for
  unknown slugs. It subclasses both `GetPaidException` and `KeyError`,
  so existing `except KeyError` code keeps working.
- `ReconciliationRequiredError` (see above).

---

## v3.0.1 (2026-06-05)

### Notes

- Version bump to coordinate with `django-getpaid` v3.0.1, which replaced
  enum inheritance with composition to support Python 3.14's stricter
  `EnumType._check_for_existing_members_` check. No changes to core enums
  themselves — the breaking change was in the Django adapter's wrapper classes.

---

## v3.0.0 (2026-06-04)

Major stable release — framework-agnostic payment processing core.

### Breaking Changes

- Complete rewrite as a framework-agnostic library, no longer coupled to Django
- `django-fsm` dependency removed — replaced by runtime FSM via `transitions`
- Requires Python 3.12+
- `can_proceed()` replaced by `may_trigger()`

### Features

- Payment status enum (`PaymentStatus`) with 9 states matching django-getpaid v2 values
- Fraud status enum (`FraudStatus`) with 4 states
- Backend method and confirmation method enums
- `BaseProcessor` abstract class for payment gateway plugins
- Semantic payment and fraud update engine
- Transition validation with `InvalidTransitionError`
- Provider metadata merging and callback idempotency tracking
- `PluginRegistry` with entry-point discovery and manual registration
- Runtime-checkable protocols: `Payment`, `Order`, `PaymentRepository`
- Dataclass response types: `BuyerInfo`, `ItemInfo`, `ChargeResult`,
  `PaymentUpdate`, `RefundResult`, `TransactionResult`
- Structured exception hierarchy with `context` support

---

## v0.1.0 (2026-02-13)

Initial release — extracted from django-getpaid v2 and redesigned as a
framework-agnostic library.

### Features

- Payment status enum (`PaymentStatus`) with 9 states matching django-getpaid v2
  values for backward compatibility
- Fraud status enum (`FraudStatus`) with 4 states
- Backend method and confirmation method enums
- `BaseProcessor` abstract class for payment gateway plugins
- Semantic payment and fraud update engine
- Transition validation with `InvalidTransitionError`
- Provider metadata merging and callback idempotency tracking
- `PluginRegistry` with entry-point discovery and manual registration
- Runtime-checkable protocols: `Payment`, `Order`, `PaymentRepository`
- Dataclass response types: `BuyerInfo`, `ItemInfo`, `ChargeResult`,
  `PaymentUpdate`, `RefundResult`, `TransactionResult`
- Structured exception hierarchy with `context` support
