# Changelog

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
