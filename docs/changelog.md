# Changelog

## v0.1.0 (2026-02-13)

Initial release — extracted from django-getpaid v2 and redesigned as a
framework-agnostic library.

### Features

- Payment status enum (`PaymentStatus`) with 9 states matching django-getpaid v2
  values for backward compatibility
- Fraud status enum (`FraudStatus`) with 4 states
- Backend method and confirmation method enums
- `BaseProcessor` abstract class for payment gateway plugins
- Payment and fraud state machines using `transitions` library
- Transition guards (`_require_fully_paid`, `_require_fully_refunded`)
- Amount callbacks (`_store_locked_amount`, `_accumulate_paid_amount`)
- Fraud message callback (`_store_fraud_message`)
- `PluginRegistry` with entry-point discovery and manual registration
- Runtime-checkable protocols: `Payment`, `Order`, `PaymentRepository`
- Typed data structures: `BuyerInfo`, `ItemInfo`, `ChargeResponse`,
  `PaymentStatusResponse`, `TransactionResult`
- Structured exception hierarchy with `context` support
