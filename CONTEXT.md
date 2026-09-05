# Context: getpaid-core

Ubiquitous language for the getpaid payment-processing core. Glossary only —
implementation decisions live in ADRs and the configured project work tracker.

## Terms

### RecurringAgreement

A standing authorization to charge a payer repeatedly over time. Covers both
recurrence shapes: **provider-managed** (the provider runs the billing
schedule, e.g. a Stripe Billing subscription, and payments arrive unprompted)
and **merchant-managed** (a stored credential / card-on-file token; the
application runs the schedule and initiates each charge). Deliberately *not*
called "Subscription": that word names the provider-side object in Stripe's
model, and no subscription exists at the provider in the merchant-managed
shape.

### managed_by

The recurrence-shape discriminator on a RecurringAgreement: **PROVIDER** (the
billing schedule runs at the provider; payments arrive unprompted) or
**MERCHANT** (the application runs the schedule against a stored credential).
Exactly two values: "who pulls the trigger for one charge" (customer-initiated
vs merchant-initiated, CIT/MIT) is a property of each charge operation, not of
the agreement — one merchant-managed agreement legitimately mixes both (e.g.
scheduled MIT charges with a customer-present CIT recovery after a decline).

### payer_id

An opaque, application-owned customer identifier carried by a
RecurringAgreement. Core never interprets it. There is deliberately no Payer
entity in core: customer identity lives above core; providers only need
identifiers (PayU `extCustomerId`, the app's own key for its Stripe Customer
mapping).

### Stored credential

The provider-side handle to a saved payment method usable for later charges
(Stripe PaymentMethod `pm_…`, PayU multi-use token `TOKC_…`). Held by a
RecurringAgreement as `payment_method_token`; may be absent on
provider-managed agreements where the provider keeps the default payment
method internally.

### external_id (agreement)

The provider-side identifier of the agreement object itself, when the
provider has one (Stripe subscription `sub_…`). `None` for merchant-managed
agreements — no provider-side agreement object exists there; the durable
handles are the provider customer id and the stored credential. Never a
transient stand-in (e.g. a SetupIntent id).

### external_id (payment)

Merchant-managed: the provider payment-object id, set by us before any
webhook (Stripe PaymentIntent, PayU order) — the one-off convention
unchanged. Provider-managed: the invoice id — one invoice = one billing
cycle = one Payment; retry attempts are events on that payment, and PI ids
ride in `provider_data`.

### Current period

The billing period a RecurringAgreement is paid through
(`current_period_start`/`current_period_end`). First-class data, not derived:
core is clockless — provider-managed agreements have these copied from
webhook payloads; merchant-managed agreements are charged by the
application's scheduler when *its* clock passes `current_period_end`.

### CIT / MIT

Customer-initiated vs merchant-initiated transaction — card-network terms for
whether the cardholder is present at charge time. Charge-scoped, never
agreement-scoped.

### Order

What the application sells; core sees it only through the `Order` protocol
(amount, currency, buyer info, items, return URL). One order may have many
payments.

### Payment

A single attempt to move money for an order through one backend. Owns the
payment lifecycle status (FSM) and the provider correlation handle
(`external_id`). May belong to a RecurringAgreement (`agreement_id`, nullable
— null means a one-off payment). Every payment has an order, including
recurring renewals: merchant-managed renewals charge against an app-created
billing-cycle order; provider-managed renewals get one materialized at
correlation time. A RecurringAgreement never references an Order.

### Billing-cycle order

The Order a renewal payment belongs to — one per billing period, created by
the application (merchant-managed) or materialized by the adapter when a
provider-initiated payment arrives (provider-managed). Keeps the "every
payment has an order" invariant universal.

### Operation intent

One deliberate instruction to a provider for a payment, distinct from attempts
to submit it. Retrying an intent is not a new capture or refund.

### Operation ID

The application-assigned identity of an operation intent within a payment,
unchanged across retries and restarts. A separate deliberate partial capture or
refund has a different operation ID even when its amount is identical.

### Operation reservation

The durable acceptance of an intent against the payment's current financial
state, including its concrete amount and exclusive right to proceed. It is not
provider acceptance or financial settlement.

### Provider acceptance

The provider's acknowledgement that it accepted an operation for processing.
For a capture or refund, acceptance does not establish that funds have settled.

### Unknown operation outcome

An operation whose provider effect cannot yet be established from available
evidence. Unknown does not mean rejected, cancelled, or safe to submit again.

### Reconciliation

Establishing and recording a payment operation's outcome and financial effects
from trustworthy provider evidence or an auditable operator resolution. It is
not a fresh financial command or an automatic compensating refund.

### Captured funds

The cumulative amount collected through a payment, before refunds. Refunding
funds does not reduce the amount historically captured.

### Refunded funds

The cumulative captured amount confirmed returned through refunds. Releasing
uncaptured authorization does not contribute to refunded funds.

### Remaining authorization

The uncaptured portion of a provider hold still available for supported capture
or release. It is distinct from captured funds and from money available to refund.

### Authorization release

Removal of a remaining uncaptured hold. It does not return captured funds and
must not be described as a refund.

### Cumulative financial observation

A report of a payment's total captured or refunded amount, rather than an
instruction to add that amount again. A repeated or older observation does not
establish newly moved money.

### Provider event identity

The identity of one provider observation within its provider/payment context,
used to recognize repeated delivery. It is distinct from the operation ID that
identifies the merchant's financial intent.

### Refund cancellation

An instruction targeting a specific pending refund to stop its unexecuted
portion, where the provider supports it. It cannot undo refunded funds already
confirmed returned.
