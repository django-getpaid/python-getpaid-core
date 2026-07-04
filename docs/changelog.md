# Changelog

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
