# Stripe Billing: object model and subscription lifecycle

Scope note: facts needed to design provider-managed recurring billing in
getpaid-core — Stripe Billing runs the schedule, payments arrive unprompted via
webhooks. Companion to
[`stripe-webhooks.md`](../../../python-getpaid-stripe/docs/research/stripe-webhooks.md)
(one-off payments; signature verification, dedup, ordering — not repeated
here). Facts only, each claim cited to docs.stripe.com (fetched 2026-07-10;
most pages fetched as their `.md` variants). The merchant-managed
(card-on-file) shape is a separate ticket.

**API-version warning up front**: Stripe API version `2025-03-31.basil`
restructured exactly the fields this design depends on (invoice↔subscription
and invoice↔PaymentIntent links, subscription period fields). Webhook payload
shape follows the endpoint's pinned API version
([Webhooks — API versions](https://docs.stripe.com/webhooks)), so both the
pre-basil and basil shapes are documented below wherever they differ.

## 1. Object model and correlation graph

### 1.1 Objects and the fields that matter

Object chain for a provider-managed subscription:

```
Product ← Price ← SubscriptionItem ∈ Subscription → (per cycle) Invoice → PaymentIntent
                                        ↑                                      ↑
                                    Customer ──────────────────────────────────┘
```

**Subscription** ([API ref](https://docs.stripe.com/api/subscriptions/object)):

- `id` (`sub_…`), `customer` — "ID of the customer who owns the subscription."
- `status` — enum `incomplete`, `incomplete_expired`, `trialing`, `active`,
  `past_due`, `canceled`, `unpaid`, `paused` (full semantics in §2).
- `items` — "List of subscription items, each with an attached price"; each
  SubscriptionItem has `id` (`si_…`), `price` (Price object, which carries
  `product`), `quantity`, and — **on basil** — `current_period_start` /
  `current_period_end`: "The start/end time of this subscription item's
  current billing period." The changelog entry ["Adds subscription item-level
  billing periods and removes subscription-level
  periods"](https://docs.stripe.com/changelog/basil/2025-03-31/deprecate-subscription-current-period-start-and-end.md)
  (breaking) removed subscription-level `current_period_start`/`end`;
  pre-basil payloads still carry them at the subscription level. A design that
  records "current billing period" must read it per-item on basil (items
  normally share a period unless intervals differ).
- `latest_invoice` — "The most recent invoice this subscription has generated
  over its lifecycle (for example, when it cycles or is updated)" (nullable,
  expandable).
- `metadata` — free-form key-value pairs (50 keys, 40-char keys, 500-char
  values — [Metadata](https://docs.stripe.com/metadata)).
- `cancel_at_period_end` — "Whether this subscription will (if
  `status=active`) or did (if `status=canceled`) cancel at the end of the
  current billing period."
- `cancel_at`, `canceled_at`, `ended_at`, `trial_start`, `trial_end` —
  timestamps; note `canceled_at` "will reflect the time of the most recent
  update request, not the end of the subscription period" when
  `cancel_at_period_end` is used.
- `trial_settings.end_behavior.missing_payment_method` — enum `cancel` /
  `create_invoice` / `pause` (§4).
- `pause_collection` — "If specified, payment collection for this subscription
  will be paused. Note that the subscription status will be unchanged and will
  not be updated to `paused`" — with `behavior` ∈ `keep_as_draft` /
  `mark_uncollectible` / `void` and `resumes_at` (§5).
- `collection_method` — `charge_automatically` or `send_invoice` (§1.3).
- `default_payment_method` — "ID of the default payment method for the
  subscription… This takes precedence over `default_source`. If neither are
  set, invoices will use the customer's
  `invoice_settings.default_payment_method` or `default_source`."
- `pending_setup_intent` — SetupIntent "to collect user authentication when
  creating a subscription without immediate payment" (trial flows).
- `pending_update` — "pending updates that will be applied to the subscription
  once the `latest_invoice` has been paid" (§6.3).
- `billing_cycle_anchor` — "The reference point that aligns future billing
  cycle dates."

**Invoice** ([API ref](https://docs.stripe.com/api/invoices/object)):

- `id` (`in_…`), `customer`, `status` — "one of `draft`, `open`, `paid`,
  `uncollectible`, or `void`" — plus `status_transitions` timestamps
  (`finalized_at`, `paid_at`, `voided_at`, `marked_uncollectible_at`).
- `billing_reason` — why the invoice exists. Subscription-relevant values
  (verbatim): `subscription_create` "A new subscription was created";
  `subscription_cycle` "A subscription advanced into a new period";
  `subscription_update` "A subscription was updated"; `subscription_threshold`
  "A subscription reached a billing threshold"; `subscription` "No longer in
  use. Applies to subscriptions from before May 2018"; plus non-subscription
  values `manual`, `quote_accept`, `upcoming` (preview only),
  `automatic_pending_invoice_item_invoice`. **`billing_reason` is the cheapest
  in-payload discriminator between a first invoice (`subscription_create`) and
  an unprompted renewal (`subscription_cycle`).**
- Link to the subscription — **API-version dependent**:
  - basil: `parent` — "The parent that generated this invoice", with
    `parent.type` ∈ `subscription_details` / `quote_details`;
    `parent.subscription_details.subscription` = "The subscription that
    generated this invoice" (the `sub_…` id, expandable) and
    `parent.subscription_details.metadata` = "Set of key-value pairs defined
    as subscription metadata when an invoice is created. Becomes an immutable
    snapshot of the subscription metadata at the time of invoice
    finalization. Note: This attribute is populated only for invoices created
    on or after June 29, 2023."
  - pre-basil: top-level `invoice.subscription` and
    `invoice.subscription_details` (with the same metadata snapshot). The
    changelog ["Invoicing resources now specify how they were
    generated"](https://docs.stripe.com/changelog/basil/2025-03-31/adds-new-parent-field-to-invoicing-objects.md)
    (breaking) removed `subscription`, `subscription_details`,
    `subscription_proration_date` and `quote` from the Invoice; migration is
    `invoice.subscription` → `invoice.parent.subscription_details.subscription`
    (checking `parent.type == "subscription_details"`). Line items likewise
    moved to `line_item.parent.subscription_item_details.subscription_item`.
- Link to the PaymentIntent — **API-version dependent**:
  - basil: no top-level `payment_intent`. Instead `payments` — "Payments for
    this invoice" (list of InvoicePayment objects, each with
    `payment.type` ∈ `payment_intent` / `charge` / `payment_record` and
    `payment.payment_intent` = "ID of the PaymentIntent associated with this
    payment"), and `confirmation_secret` — "contains the client_secret of the
    PaymentIntent that Stripe creates during invoice finalization"
    (`confirmation_secret.type` is "always payment_intent, referencing the
    default payment_intent that Stripe creates during invoice finalization").
  - pre-basil: top-level `invoice.payment_intent` (and `invoice.charge`,
    `invoice.paid`). The changelog ["Adds support for multiple (partial)
    payments on
    invoices"](https://docs.stripe.com/changelog/basil/2025-03-31/add-support-for-multiple-partial-payments-on-invoices.md)
    (breaking) removed `invoice.payment_intent`, `invoice.charge`,
    `invoice.paid`, `invoice.paid_out_of_band` — **and removed
    `payment_intent.invoice` and `charge.invoice`**. On basil, the reverse
    lookup (PI → invoice) is the [List InvoicePayments
    endpoint](https://docs.stripe.com/api/invoice-payment/list):
    `GET /v1/invoice_payments?payment[type]=payment_intent&payment[payment_intent]=pi_…`.
    New event: `invoice.overpaid`; InvoicePayments also emit
    `invoice_payment.paid` ("Occurs when an InvoicePayment is successfully
    paid" — [event types](https://docs.stripe.com/api/events/types)).
- `metadata` — the invoice's own metadata (empty unless someone sets it; the
  subscription snapshot lives in `parent.subscription_details.metadata`, not
  here).
- `auto_advance` — "Controls whether Stripe performs automatic collection of
  the invoice. If `false`, the invoice's state doesn't automatically advance
  without an explicit action."
- `next_payment_attempt` — "The time at which payment will next be attempted.
  This value will be `null` for invoices where
  `collection_method=send_invoice`."
- `attempt_count` — "Number of payment attempts made for this invoice, from
  the perspective of the payment retry schedule. Any payment attempt counts as
  the first attempt, and subsequently only automatic retries increment the
  attempt count… If a failure is returned with a non-retryable return code,
  the invoice can no longer be retried unless a new payment method is
  obtained."
- `attempted` — "Whether an attempt has been made to pay the invoice. An
  invoice is not attempted until 1 hour after the `invoice.created` webhook,
  for example, so you might not want to display that invoice as unpaid to your
  users."
- `period_start` / `period_end` — invoice-item association bounds; "Use the
  line item period to get the service period for each price" (the true
  service period lives on line items).
- `hosted_invoice_url` — customer-facing pay page; "If the invoice has not
  been finalized yet, this will be null."

**PaymentIntent** ([API ref](https://docs.stripe.com/api/payment_intents/object)):
on basil the PaymentIntent object has **no `invoice` field and no
subscription-related field at all**; its `metadata` is the generic key-value
description with no documented copy from invoice or subscription. This is the
crux of the correlation problem (§1.2, §7.4).

**Customer / Price / Product**: the Customer (`cus_…`) is the stable
cross-cycle anchor — every subscription, invoice, and subscription-spawned
PaymentIntent carries `customer`. Price (`price_…`) carries `product`
(`prod_…`); Stripe's own provisioning guidance is to key entitlements on the
product: "Check the product the customer subscribed to and grant access to
your service. Checking the product instead of the price gives you more
flexibility if you need to change the pricing or billing period"; store
"`product.id`, `subscription.id` and `subscription.status` … along with …
the `customer.id`"
([Build subscriptions](https://docs.stripe.com/billing/subscriptions/build-subscriptions)).

### 1.2 Metadata propagation ([Metadata](https://docs.stripe.com/metadata))

General rule: "An object's metadata doesn't automatically copy to related
objects." Documented one-time-snapshot exceptions and nested setters relevant
here:

| From → To | Mechanism | Quote |
|---|---|---|
| Checkout Session → Subscription | `subscription_data.metadata` at session creation | "Data you include with the `subscription_data.metadata` attribute saves to the underlying Subscription's metadata." |
| Subscription → Invoice | automatic snapshot | "When a Subscription creates an Invoice, the metadata copies to the Invoice object's `parent.subscription_details.metadata` attribute in a one-time snapshot. Updates to the subscription's metadata won't apply to the Invoice." |
| Invoice/Subscription → PaymentIntent | **none documented** | The metadata page documents no propagation onto PaymentIntents created during invoice finalization, and the PaymentIntent API ref carries no note either. |
| PaymentIntent → Charge | automatic snapshot | "When a PaymentIntent creates a Charge, the metadata copies to the Charge in a one-time snapshot." |
| Payment Link → Subscription | `subscription_data.metadata` | "saves to the metadata of each Subscription created by the Payment Link." |
| Subscription Schedule → Subscription | `phases.metadata` | "saves to the underlying Subscription's metadata when the phase is entered." |

Consequence: **merchant metadata put on the subscription reaches every
invoice (as `parent.subscription_details.metadata`) but never reaches the
PaymentIntent** that pays the invoice. Also note `payment_intent_data` on
Checkout Sessions is "A subset of parameters to be passed to PaymentIntent
creation for Checkout Sessions in `payment` mode"
([Create a Session](https://docs.stripe.com/api/checkout/sessions/create)) —
i.e. the one-off plugin's trick of stamping PI metadata via
`payment_intent_data[metadata]` is **not available in `subscription` mode**.

The subscription-metadata snapshot timing has a subtlety: the invoicing guide
says "The invoice's subscription_details.metadata attribute always contains
the subscription's metadata at the time of invoice creation, even if the
subscription metadata is later modified"
([Subscription invoices](https://docs.stripe.com/billing/invoices/subscription)),
while the API ref says the snapshot "Becomes an immutable snapshot … at the
time of invoice finalization"
([Invoice object](https://docs.stripe.com/api/invoices/object)). Creation vs
finalization is ~1 hour apart on renewals; the docs are inconsistent about
which instant wins, so don't design anything that depends on metadata edits
landing inside that window.

### 1.3 How invoices spawn PaymentIntents unprompted

Renewal machinery, verbatim
([Subscription invoices](https://docs.stripe.com/billing/invoices/subscription)):

> "When subscriptions renew, Stripe:
> - Creates an invoice.
> - Leaves the invoice in a `draft` status for about an hour.
> - Attempts to finalize and pay the invoice with the default payment method.
> - Changes the invoice status to `paid` if payment succeeds."

The one-hour draft window exists so the merchant can edit the renewal invoice:
"When Stripe creates an invoice, you receive an `invoice.created` event…
the invoice status is `draft`, which means that its invoice items are open for
modification"; "Stripe waits approximately one hour before finalizing the
invoice and attempting payment, or sending an email." The webhooks guide adds
two flow-control facts: "Stripe waits an hour after receiving a successful
response to the `invoice.created` event before attempting payment" and "If
Stripe fails to receive a successful response to `invoice.created`, then
finalizing all invoices with automatic collection is delayed for up to 72
hours"
([Subscription webhooks](https://docs.stripe.com/billing/subscriptions/webhooks)).
So a *failing* webhook endpoint delays everyone's renewals — the handler for
`invoice.created` must 2xx promptly.

The **first** invoice is different: "When you create a subscription for a
customer, Stripe: Creates an invoice. Finalizes the invoice immediately when
collection_method is set to `charge_automatically`, or one hour later when
it's set to `send_invoice`"
([Subscription invoices](https://docs.stripe.com/billing/invoices/subscription)).
No draft window on the first invoice under `charge_automatically`.

The PaymentIntent is born at **finalization**: the invoice's
`confirmation_secret` "contains the client_secret of the PaymentIntent that
Stripe creates during invoice finalization"
([Invoice object](https://docs.stripe.com/api/invoices/object)). So the
unprompted renewal sequence is: billing cycle boundary → draft invoice
(`invoice.created`, `billing_reason=subscription_cycle`) → ~1 h → finalization
(`invoice.finalized`, PaymentIntent created, `payment_intent.created`) →
automatic payment attempt (`invoice.paid` + `payment_intent.succeeded`, or
`invoice.payment_failed` + `payment_intent.payment_failed`, or
`invoice.payment_action_required` on 3DS).

`collection_method` ([Subscription object](https://docs.stripe.com/api/subscriptions/object);
[Subscription invoices](https://docs.stripe.com/billing/invoices/subscription)):

- `charge_automatically` (default): "Stripe will attempt to pay this
  subscription at the end of the cycle using the default source attached to
  the customer." Automatic PaymentIntent, Smart Retries apply,
  `next_payment_attempt` populated.
- `send_invoice`: "Stripe will email your customer an invoice with payment
  instructions and mark the subscription as `active`"; `days_until_due`
  applies; `next_payment_attempt` "will be `null`"; "If
  `collection_method=send_invoice`, Stripe doesn't send an `invoice.upcoming`
  event" ([Subscription webhooks](https://docs.stripe.com/billing/subscriptions/webhooks)).
  Payment arrives whenever the customer pays the hosted invoice page.

## 2. Subscription status graph

Source for all quotes unless noted:
[Subscription object — status](https://docs.stripe.com/api/subscriptions/object)
and [How subscriptions work](https://docs.stripe.com/billing/subscriptions/overview).

| Status | Entry | Exit | Terminal? |
|---|---|---|---|
| `incomplete` | Creation when first payment fails / requires action / is `processing`, or `payment_behavior=default_incomplete` | → `active` on payment; → `incomplete_expired` after 23 h | no |
| `incomplete_expired` | 23 h pass without successful first payment | none | **yes** |
| `trialing` | Created with a trial | → `active` at trial end (or `paused`/`canceled`/invoice-then-`past_due` per trial settings) | no |
| `active` | First payment succeeds; trial converts; past_due invoice paid; paused sub resumed | → `past_due` on failed renewal; → `canceled`; (→ `unpaid` only via `past_due`) | no |
| `past_due` | Renewal payment "either failed or wasn't attempted" | → `active` if latest invoice paid/marked uncollectible; after retries exhaust → `canceled` or `unpaid` or stays `past_due` (dashboard setting) | no |
| `canceled` | Immediate cancel; `cancel_at_period_end`/`cancel_at` reached; retries exhausted with cancel setting; trial ends w/o PM with `cancel` behavior | none — "This is a terminal state that can't be updated." | **yes** |
| `unpaid` | Retries exhausted with "mark unpaid" setting (or `send_invoice` deadline passed) | → `active` by paying most recent invoice | no |
| `paused` | Trial ends w/o payment method and `trial_settings.end_behavior.missing_payment_method=pause` | → `active` via [resume endpoint](https://docs.stripe.com/api/subscriptions/resume) after attaching a PM | no |

Verbatim per-status facts worth keeping exact:

- **incomplete**: "The customer must make a successful payment within 23 hours
  to activate the subscription. Or the payment requires action, such as
  customer authentication. Subscriptions can also be `incomplete` if there's a
  pending payment and the PaymentIntent status is `processing`." While
  incomplete, "A subscription in this status can only have metadata and
  default_source updated." The 23-hour window "accommodates customers who pay
  while on-session… If the customer returns to your application after 23
  hours, create a new subscription for them"
  ([overview](https://docs.stripe.com/billing/subscriptions/overview)).
- **incomplete_expired**: "If the first invoice is not paid within 23 hours,
  the subscription transitions to `incomplete_expired`. This is a terminal
  status, the open invoice will be voided and no further invoices will be
  generated." "This status exists so you can track customers that failed to
  activate their subscriptions."
- **trialing**: "you can safely provision your product for your customer. The
  subscription transitions automatically to `active` when a customer makes the
  first payment."
- **active**: "Note that `active` doesn't indicate that all outstanding
  invoices associated with the subscription have been paid."
- **past_due**: "Payment on the latest *finalized* invoice either failed or
  wasn't attempted. The subscription continues to create invoices. Your
  Dashboard subscription settings determine the subscription's next status…
  you can configure the subscription to move to `canceled`, `unpaid`, or leave
  it as `past_due`. To reactivate the subscription, have your customer pay the
  most recent invoice… The subscription status becomes `active` regardless of
  whether the payment is done before or after the latest invoice due date."
  Also from the API ref: paying the latest invoice **or marking it
  uncollectible** transitions `past_due` → `active`.
- **canceled**: "During cancellation, automatic collection for all unpaid
  invoices is disabled (`auto_advance=false`)." "You can't reactivate a
  canceled subscription. You must create a new subscription"
  ([Cancel subscriptions](https://docs.stripe.com/billing/subscriptions/cancel)).
- **unpaid**: "The latest invoice remains open and invoices continue to
  generate, but payments aren't attempted. Revoke access to your product when
  the subscription is `unpaid` because payments were already attempted and
  retried while `past_due`." API ref: "no subsequent invoices will be
  attempted (invoices will be created, but then immediately automatically
  closed)."
- **paused**: "Invoices are no longer created for the subscription. After
  attaching a default payment method to the customer, you can resume the
  subscription." Explicitly: "The `paused` status is different from pausing
  collection, which still generates invoices and leaves the subscription's
  status unchanged" ([API ref](https://docs.stripe.com/api/subscriptions/object)).
  Resume mechanics: "If Stripe doesn't generate a resumption invoice, the
  subscription becomes `active` immediately. When a resumption invoice is
  generated, Stripe finalizes it immediately. If the invoice is paid or marked
  uncollectible, the subscription becomes `active`. If the invoice is manually
  voided, the subscription stays `paused`. If there is no payment attempt
  within 23 hours, Stripe voids the invoice and the subscription stays
  `paused`" ([Resume a subscription](https://docs.stripe.com/api/subscriptions/resume)).

`send_invoice` variant of the dunning edges: "it becomes `past_due` when its
invoice is not paid by the due date, and `canceled` or `unpaid` if it is still
not paid by an additional deadline after that"
([API ref](https://docs.stripe.com/api/subscriptions/object)).

Cancellation specifics ([Cancel subscriptions](https://docs.stripe.com/billing/subscriptions/cancel)):

- Immediate: `DELETE /v1/subscriptions/:id` — "cancellation takes effect
  immediately and invoices are no longer generated"; optional final invoice
  for prorations/metered usage "using the `invoice_now` parameter".
- `cancel_at_period_end=true` — "allows the subscription to complete the
  duration of time the customer has already paid for"; reversible: "You can
  reactivate subscriptions scheduled for cancellation by updating
  `cancel_at_period_end` to `false`… at any time up to the end of the period."
- `cancel_at=<ts>` — "When you schedule a cancel date that occurs before the
  billing period ends, the subscription's items' `current_period_end` updates
  to match the `cancel_at` date."
- Events: `customer.subscription.updated` is "Sent for any subscription
  update, including when `cancel_at_period_end` is set to `true`";
  `customer.subscription.deleted` is "Sent when a subscription is canceled.
  The cancellation can result from a direct call to delete the subscription or
  when a subscription with `cancel_at_period_end: true` reaches the end of its
  billing period." So *scheduling* a cancel is an `updated` event; the status
  only becomes `canceled` (and `deleted` fires) at the effective moment.

## 3. Creation paths

### 3.1 Checkout Session `mode=subscription`

- `mode=subscription`: "Pass `subscription` if the Checkout Session includes
  at least one recurring item"; enum text: "Use Stripe Billing to set up
  fixed-price subscriptions"
  ([Create a Session](https://docs.stripe.com/api/checkout/sessions/create)).
- The completed Session references the created objects: the Session object has
  a nullable, expandable `subscription` field (the `sub_…` id for
  subscription-mode sessions) and an `invoice` field — "ID of the invoice
  created by the Checkout Session, if it exists"
  ([Session object](https://docs.stripe.com/api/checkout/sessions/object)).
  `session.payment_intent` is documented as "The ID of the PaymentIntent for
  Checkout Sessions in `payment` mode" — i.e. in subscription mode the first
  payment's PI hangs off the **invoice**, not the session.
- `customer`: "For Checkout Sessions in `subscription` mode…, Checkout will
  create a new customer object based on information provided during the
  payment flow unless an existing customer was provided"
  ([Session object](https://docs.stripe.com/api/checkout/sessions/object)).
- Metadata: `subscription_data.metadata` "saves to the underlying
  Subscription's metadata" ([Metadata](https://docs.stripe.com/metadata));
  `payment_intent_data` is payment-mode only (§1.2). `client_reference_id`
  and session `metadata` behave as for one-off sessions.
- How the checkout-based plugin learns the subscription id: the
  `checkout.session.completed` payload's `data.object` is the Session, so
  `session.subscription` is available there; Stripe's guide: "You can listen
  to the `checkout.session.completed` event to make the update after the
  session has completed"
  ([overview](https://docs.stripe.com/billing/subscriptions/overview)). The
  same purchase also emits `customer.subscription.created`,
  `invoice.created`/`invoice.finalized`/`invoice.paid`
  (`billing_reason=subscription_create`), and `payment_intent.*` for the first
  invoice's PI.

### 3.2 Direct API creation

`POST /v1/subscriptions` with `payment_behavior` controlling first-invoice
handling ([Create a subscription](https://docs.stripe.com/api/subscriptions/create)),
verbatim:

- `allow_incomplete` — "the default behavior since 2019-03-14. If payment
  fails, the Subscription is created with `status=incomplete`, otherwise
  `status=active`."
- `default_incomplete` — "When the first invoice requires payment, creates a
  Subscription with `status=incomplete` without attempting payment, otherwise
  `status=active`. You must request explicit confirmation of the Invoice's
  PaymentIntent to activate the subscription. The resulting Invoice has
  `auto_advance=false`, so Stripe doesn't automatically attempt payment, retry
  payment, or finalize the subscription." This is the recommended build path:
  "create the subscription with an `incomplete` status using
  `payment_behavior=default_incomplete`. Then, return the `client_secret` from
  the subscription's first PaymentIntent to the front end to complete payment…
  expand the confirmation_secret on the latest invoice"
  ([Build subscriptions](https://docs.stripe.com/billing/subscriptions/build-subscriptions)).
- `error_if_incomplete` — "If payment fails, return an HTTP `402` status code
  and don't create the subscription… doesn't support payments that require
  user action."
- `pending_if_incomplete` — "exclusive to Subscription updates and cannot be
  used for creation" (§6.3).
- `payment_behavior` only matters for `charge_automatically`: "Subscriptions
  with `collection_method=send_invoice` are automatically activated regardless
  of the first Invoice status."

Both paths converge on the same webhook stream afterwards; Stripe's
recommended minimum listen set for either path is
`customer.subscription.created/updated/deleted`, `invoice.paid`,
`invoice.payment_failed`
([Build subscriptions](https://docs.stripe.com/billing/subscriptions/build-subscriptions)).

## 4. Trials

- Parameters ([Create a subscription](https://docs.stripe.com/api/subscriptions/create)):
  `trial_period_days` — "number of trial period days before the customer is
  charged for the first time. This will always overwrite any trials that might
  apply via a subscribed plan"; `trial_end` — "Unix timestamp representing the
  end of the trial period… The special value `now` can be provided to end the
  customer's trial immediately. Can be at most two years from
  `billing_cycle_anchor`." On Checkout, trials go through
  `subscription_data`.
- `trial_settings.end_behavior.missing_payment_method` — "Indicates how the
  subscription should change when the trial ends if the user did not provide
  a payment method": `cancel` — "Cancel the subscription if a payment method
  is not attached when the trial ends"; `create_invoice` — "Create an invoice
  when the trial ends, even if the user did not set up a payment method"
  (invoice then fails → normal dunning path); `pause` — "Pause the
  subscription if a payment method is not attached when the trial ends"
  ([Subscription object](https://docs.stripe.com/api/subscriptions/object)).
- Status during trial is `trialing`; "you can safely provision"; it "moves to
  `active` when the trial period is over" (with a payment method, the
  trial-end invoice is created and charged like any cycle invoice).
- `customer.subscription.trial_will_end` — "Occurs three days before a
  subscription's trial period is scheduled to end, or immediately when a trial
  is ended early (for example, with `trial_end=now`…). If a trial is shortened
  so that fewer than three days remain, this event can fire immediately,
  including during the same transaction that collects payment. Before sending
  payment-reminder communications from this webhook, check the subscription
  status and latest invoice to determine whether payment has already been
  collected" ([event types](https://docs.stripe.com/api/events/types)).
  Recommended action: "verify that you have a payment method on the customer
  so you can bill them"
  ([Subscription webhooks](https://docs.stripe.com/billing/subscriptions/webhooks)).
- Trial-end events by outcome: `cancel` behavior →
  `customer.subscription.deleted`; `pause` behavior →
  `customer.subscription.paused`; with a payment method → the normal
  `customer.subscription.updated` (trialing→active) plus cycle-invoice events
  ([trials](https://docs.stripe.com/billing/subscriptions/trials);
  [event types](https://docs.stripe.com/api/events/types)).
- **Doc-state caveat**: the trials page has been restructured around a new
  "Trial Offer API" (trial offers as 0-USD or discounted prices; "You can't
  use trial offers and the legacy `trial_end` parameter together", and
  Checkout still requires "legacy free trials with `trial_end`")
  ([Trials](https://docs.stripe.com/billing/subscriptions/trials)). The
  classic `trial_period_days`/`trial_end`/`trial_settings` parameters remain
  live in the API reference. Under the Trial Offer API, a subscription whose
  trial offers have non-zero prices is *not* `trialing` — "the subscription
  status will be `active`, `incomplete`, or `past_due`… Because a paid trial
  requires an immediate successful payment"; only all-0-USD trial offers give
  `trialing`. A design should treat `trialing` as "one possible pre-active
  state", not assume every trial produces it.

## 5. Pause and resume — two distinct mechanisms

Source: [Pause payment collection](https://docs.stripe.com/billing/subscriptions/pause-payment)
unless noted.

**Mechanism A — `pause_collection` (status unchanged).** "Pausing payment
collection keeps the subscription `active`, invoices still generate, and only
collection pauses. Your customers keep access to the service while you pause
collection." `behavior` values, verbatim:

- `void` — "All invoices created before the `resumes_at` date are immediately
  marked as void. Stripe won't send any upcoming invoice emails or webhooks
  and the subscription's status remains unchanged."
- `keep_as_draft` — "All invoices created before the `resumes_at` date remain
  in `draft` status and `auto_advance` is set to `false`… the subscription's
  status remains unchanged." (Collectable later by flipping
  `auto_advance=true` per invoice.)
- `mark_uncollectible` — "we'll stop active payment collection on new
  invoices… Stripe applies any existing customer balance to invoices. If the
  invoice's `total` is paid off entirely using customer balance, then the
  invoice's status is set to `paid`. Otherwise, the invoice's status is set to
  `uncollectible`."

`resumes_at` optionally auto-resumes; otherwise "the subscription remains
paused until you unset `pause_collection`" (update with empty
`pause_collection`; "Resuming collection this way only affects future
invoices"). Because status never changes, the only webhook signal is
`customer.subscription.updated` with the `pause_collection` hash — the
dedicated events explicitly exclude it: `customer.subscription.paused` "Only
applies when subscriptions enter `status=paused`, not when payment collection
is paused"; same wording for `.resumed`
([event types](https://docs.stripe.com/api/events/types)).

**Mechanism B — `paused` status.** "Pausing a subscription is different,
because it stops both service and billing. The subscription moves to `paused`
status, Stripe stops generating invoices, and your customer loses access."
Enter only via trial-end-without-PM (§2, §4); exit via the resume endpoint
(§2), which "Initiates resumption of a paused subscription, optionally
resetting the billing cycle anchor and creating prorations"
([Resume](https://docs.stripe.com/api/subscriptions/resume)); emits
`customer.subscription.resumed`.

## 6. Plan changes and proration

### 6.1 Updating items

Replace the price by targeting the existing SubscriptionItem: "You must
specify the subscription item to replace the current price with the new price.
Failing to do so results in *adding* the new price so both prices are active";
"Updating a subscription price automatically reverts the quantity to the
default value of `1`… you must include it in the update" to preserve it
([Change price](https://docs.stripe.com/billing/subscriptions/change-price)).
Any plan change fires `customer.subscription.updated` ("switching from one
plan to another" is the event's canonical example —
[event types](https://docs.stripe.com/api/events/types)).

### 6.2 Proration ([Prorations](https://docs.stripe.com/billing/subscriptions/prorations))

Triggers: changing items/price/quantity, adding `trial_end`, changing
`billing_cycle_anchor`, setting `cancel_at` mid-period. `proration_behavior`:

- `create_prorations` (default) — "creates proration invoice items when
  applicable. These proration items are only invoiced immediately under
  certain conditions" — otherwise they ride the **next** cycle invoice
  (which then has `billing_reason=subscription_cycle` and mixed line items).
- `always_invoice` — "calculates the proration, then immediately generates an
  invoice" (`billing_reason=subscription_update`). This is the upgrade
  charge-now recipe; docs recommend combining with pending updates "so the
  subscription doesn't update unless payment succeeds on the new invoice"
  ([Change price](https://docs.stripe.com/billing/subscriptions/change-price)).
- `none` — "Disable creating prorations in this request… customers are billed
  the full amount at the new price when the next invoice is generated."

`proration_date`: "Because Stripe prorates to the second, prorated amounts
might change between the time they're previewed and the time the update is
made. To avoid this, pass in a `subscription_details.proration_date` value
when creating a preview… pass the same date using the `proration_date`
parameter" on the update. Proration line items are flagged `proration: true`
and `discountable: false`; on `billing_mode=flexible` subscriptions credit
prorations carry `proration_details.credited_items` to "reconcile credits
against the original charges". Credit calculation differs by `billing_mode`
(`classic`: "based on the current price"; `flexible`: "based on the last price
billed") — version-dependent behavior to not hard-code.

Billing dates after a price change: "If both prices have the same billing
periods…, the subscription retains the same billing dates. If the prices have
different billing periods, the new price is billed at the new interval,
starting on the day of the change"
([Change price](https://docs.stripe.com/billing/subscriptions/change-price)).
Resetting the cycle: "Stripe immediately attempts payment when a
subscription's billing cycle anchor is reset" (`billing_cycle_anchor=now`).
Downgrades-at-period-end are Stripe's subscription-schedules territory, not a
native "pending downgrade" flag.

### 6.3 Pending updates ([Pending updates](https://docs.stripe.com/billing/subscriptions/pending-updates))

`payment_behavior=pending_if_incomplete` on **update**: changes apply "only if
payment succeeds on the new invoice. If payment fails, the subscription
remains unchanged" and the changes sit in the subscription's `pending_update`
hash (with `expires_at`). "A pending update expires and is automatically
voided after 23 hours from the update request, or at the first occurrence of
the trial end or items' current period end." Supported attributes include
`items` (price/quantity), `billing_cycle_anchor`, `trial_end`, `metadata`,
discounts, `add_invoice_items`. Events:
`customer.subscription.pending_update_applied` ("pending update is applied,
and the subscription is updated") and
`customer.subscription.pending_update_expired` ("expires before the related
invoice is paid") ([event types](https://docs.stripe.com/api/events/types)).
`latest_invoice` "identifies the invoice the update created. Use this ID to
void the invoice if you need to cancel a pending update."

## 7. Webhook event set

Descriptions verbatim from
[Types of events](https://docs.stripe.com/api/events/types) and the
[subscription webhooks guide](https://docs.stripe.com/billing/subscriptions/webhooks).
Ordering/dedup ground rules from the one-off research still apply: "Stripe
doesn't guarantee the delivery of events in the order that they're generated"
([Webhooks](https://docs.stripe.com/webhooks)); dedup by `event.id`.

### 7.1 `customer.subscription.*` — `data.object` is the Subscription

| Event | Fires when | State-bearing? |
|---|---|---|
| `created` | "whenever a customer is signed up for a new plan." Guide: "The subscription `status` might be `incomplete` if customer authentication is required… or if you set `payment_behavior` to `default_incomplete`." | yes — initial status |
| `updated` | "whenever a subscription changes (e.g., switching from one plan to another, or changing the status from trial to active)." Renewal period advance, `cancel_at_period_end` set, `pause_collection` set/unset, item/discount changes — all arrive here. | **yes — the workhorse.** `data.object.status` + `previous_attributes` carry every non-terminal transition |
| `deleted` | "whenever a customer's subscription ends" — immediate delete or `cancel_at_period_end` reached | yes — terminal `canceled` |
| `paused` | "whenever a customer's subscription is paused. Only applies when subscriptions enter `status=paused`, not when payment collection is paused." | yes |
| `resumed` | "whenever a customer's subscription is no longer paused. Only applies when a `status=paused` subscription is resumed, not when payment collection is resumed." | yes |
| `trial_will_end` | "three days before a subscription's trial period is scheduled to end, or immediately when a trial is ended early" | no — advisory/notification |
| `pending_update_applied` / `pending_update_expired` | pending update resolved (§6.3) | semi — reconciliation hint; the applied state also arrives via `updated` |

### 7.2 `invoice.*` — `data.object` is the Invoice

| Event | Fires when | Design significance |
|---|---|---|
| `invoice.created` | "whenever a new invoice is created" — renewal draft opens the ~1 h window | State-bearing for bookkeeping: this is the earliest handle on an upcoming unprompted charge (`billing_reason=subscription_cycle`, `parent.subscription_details.subscription`). Must 2xx fast — failure "delays finalizing all invoices with automatic collection for up to 72 hours" |
| `invoice.finalized` | "whenever a draft invoice is finalized and updated to be an open invoice" — the PaymentIntent now exists | The correlation anchor: on basil expandable `payments` / `confirmation_secret`; amount and number frozen |
| `invoice.finalization_failed` | "whenever a draft invoice cannot be finalized. See the invoice's last finalization error" (mostly Stripe Tax location issues) | error path; invoice stuck in `draft` |
| `invoice.paid` | "whenever an invoice payment attempt succeeds or an invoice is marked as paid out-of-band." | **authoritative success signal** for a billing period; guide: "You can provision access… when you receive this event and the subscription `status` is `active`" |
| `invoice.payment_succeeded` | "whenever an invoice payment attempt succeeds." | subset of `invoice.paid` — does *not* cover out-of-band; prefer `invoice.paid` |
| `invoice.payment_failed` | "whenever an invoice payment attempt fails, due to either a declined payment, including soft decline, or to the lack of a stored payment method." | **authoritative failure/dunning signal**; payload carries `attempt_count` and (see §8 caveat) `next_payment_attempt`. First-invoice failure leaves the sub `incomplete`; renewal failure makes it `past_due` |
| `invoice.payment_action_required` | "whenever an invoice payment attempt requires further user action to complete" (3DS off-session) | merchant must bring the customer on-session (hosted invoice page / confirm PI) |
| `invoice.upcoming` | "X number of days before a subscription is scheduled to create an invoice that is automatically charged… **Note: The received `Invoice` object will not have an invoice ID.**" Not sent for `send_invoice` | informational only — a forecast, not an object; last chance to add invoice items. No id ⇒ nothing to correlate or store as an entity |
| `invoice.updated` | "whenever an invoice changes (e.g., the invoice amount)." Guide: fires on payment success/failure with `paid`/`status` updated | noisy; treat as reconciliation, not a trigger |
| `invoice.marked_uncollectible` / `invoice.voided` | "whenever an invoice is marked uncollectible" / "voided" | terminal invoice outcomes (write-off / annulment); voiding the latest invoice does **not** by itself reactivate anything |
| `invoice.sent`, `invoice.will_be_due`, `invoice.deleted`, `invoice.overpaid` | email sent / Automations pre-due notice / draft deleted / overpayment (basil) | peripheral |
| `invoice_payment.paid` | "when an InvoicePayment is successfully paid" (basil resource) | alternative PI↔invoice join signal |

### 7.3 `payment_intent.*` from subscription invoices

Invoice finalization creates a PaymentIntent (§1.3), so each automatic cycle
generates the normal PI stream — `payment_intent.created` at finalization,
then `payment_intent.succeeded` / `payment_intent.payment_failed` /
`payment_intent.requires_action` per attempt, and each Smart Retry is a new
attempt on the same PI (new attempts on the invoice increment
`attempt_count`; the invoice keeps at most one "default" PI —
`confirmation_secret` always references "the default payment_intent that
Stripe creates during invoice finalization",
[Invoice object](https://docs.stripe.com/api/invoices/object)).

### 7.4 Telling a subscription-invoice PI apart from a one-off checkout PI

This is the design's key discrimination problem. Facts:

- The one-off plugin's PIs are born from Checkout with
  `payment_intent_data[metadata]` and are correlated by `pi_…` ==
  `Payment.external_id` (existing behavior; see `stripe-webhooks.md` §4).
- A subscription-invoice PI has **no merchant metadata** (no documented
  propagation — §1.2), and on basil **no `invoice` field** (§1.1). On
  pre-basil API versions the PI payload carries `invoice` (`in_…`), which is a
  direct discriminator — version-dependent, so don't rely on it exclusively.
- Robust basil-era discrimination options: (a) a `payment_intent.*` event
  whose PI id matches no known `external_id` and no local prepared payment is
  *presumptively* subscription/invoice traffic — confirm via
  `GET /v1/invoice_payments?payment[type]=payment_intent&payment[payment_intent]=pi_…`
  ([basil changelog](https://docs.stripe.com/changelog/basil/2025-03-31/add-support-for-multiple-partial-payments-on-invoices.md));
  or (b) don't key subscription money-movement on `payment_intent.*` at all —
  key it on `invoice.*` events, whose payloads always carry the subscription
  id (`parent.subscription_details.subscription`), the metadata snapshot
  (`parent.subscription_details.metadata`), `billing_reason`, and (expandable)
  the PI id via `payments`/`confirmation_secret`. The invoice, not the PI, is
  the object Stripe designed for this correlation.
- Endpoint-level separation is also available: endpoints subscribe to chosen
  event types (`enabled_events`,
  [Webhooks](https://docs.stripe.com/webhooks)), so a deployment can route
  `invoice.*`/`customer.subscription.*` separately from `payment_intent.*` if
  needed.

## 8. Smart Retries and dunning as Stripe runs it

Source: [Smart Retries](https://docs.stripe.com/billing/revenue-recovery/smart-retries).

- What it is: "Using AI, Smart Retries chooses the best times to retry failed
  payment attempts to increase the chance of successfully paying an invoice,"
  using "time-dependent, dynamic signals" (device counts, local-time success
  patterns).
- Schedule: configurable as "retry payment a specific number of times within a
  time period: 1 week, 2 weeks, 3 weeks, 1 month, or 2 months"; the default
  recommendation is **8 retries within 2 weeks**. Alternative: disable Smart
  Retries and use custom rules — "You can configure up to three retries, each
  with a specific number of days after the previous attempt."
- Applies to automatic collection; for `send_invoice` there is no retry
  schedule (`next_payment_attempt` is null — §1.1) — dunning there is due
  dates plus reminder emails.
- After the final failure: "After the final payment attempt, we make no
  further payment attempts." The subscription's fate is the dashboard
  setting, verbatim: **Cancel** — "changes to a `canceled` state after the
  maximum number of days defined in the retry schedule"; **Mark unpaid** —
  "changes to an `unpaid` state… Invoices continue to be generated and stay in
  a draft state"; **Leave past-due** — "remains in a `past_due` state…
  Invoices continue to be generated and charge the customer based on retry
  settings." (Same three-way switch quoted in the
  [overview](https://docs.stripe.com/billing/subscriptions/overview):
  "If the invoice is still unpaid after all attempted smart retries, you can
  configure the subscription to move to `canceled`, `unpaid`, or leave it as
  `past_due`.")
- Signals per retry: "Use the `invoice.payment_failed` webhook to receive
  subscription payment failure events and retry attempt updates";
  `attempt_count` rides that event. **Caveat**: "For automations users,
  [`next_payment_attempt`] is set in `invoice.updated` webhooks, not
  `invoice.payment_failed`" — so the next-retry timestamp may arrive on a
  different event than the failure itself.
- Division of labor: Stripe automates retry timing, the past_due→terminal
  transition, and (configurable in the same dashboard area) failed-payment
  emails with hosted-invoice links. The merchant is expected to: react to
  `invoice.payment_failed` (notify, gate features), collect a new payment
  method and either confirm the PI or "Update the default payment method on
  the subscription"
  ([subscription webhooks guide](https://docs.stripe.com/billing/subscriptions/webhooks)),
  and decide entitlement policy per status (`past_due` = grace, `unpaid` =
  "Revoke access… because payments were already attempted and retried while
  `past_due`" — [overview](https://docs.stripe.com/billing/subscriptions/overview)).

## 9. Implications for getpaid-core design

Load-bearing conclusions the design tickets can build on:

1. **The Invoice is the correlation object, not the PaymentIntent.** Every
   `invoice.*` payload carries the subscription id
   (`parent.subscription_details.subscription` on basil,
   `invoice.subscription` pre-basil), an immutable snapshot of subscription
   metadata (`parent.subscription_details.metadata` — set
   `subscription_data.metadata` at Checkout and it reaches every future
   invoice), and `billing_reason` distinguishing first invoice
   (`subscription_create`) from unprompted renewals (`subscription_cycle`)
   and plan-change invoices (`subscription_update`). PaymentIntents spawned by
   invoices carry **no merchant metadata and (on basil) no invoice pointer**;
   the reverse join is an API call (`GET /v1/invoice_payments?payment[payment_intent]=…`).
   Therefore: for provider-managed subscriptions the plugin should treat
   `invoice.paid` / `invoice.payment_failed` / `invoice.payment_action_required`
   as the money-movement authority, and treat `payment_intent.*` for unknown
   `pi_…` ids as non-authoritative noise (or a trigger for an invoice-payments
   lookup). This inverts the one-off plugin's "payment_intent.* is
   authoritative" rule; the two rules can coexist because one-off PIs are
   recognizable by `external_id` match (or plugin-stamped
   `payment_intent_data.metadata`), and subscription PIs by the absence of
   both.
2. **Local record creation hook**: `invoice.created` with
   `billing_reason=subscription_cycle` is the earliest signal of an upcoming
   unprompted charge (~1 h before finalization/attempt; first invoices skip
   the window under `charge_automatically`). A per-period local payment
   record can be created there, keyed by `in_…`, and promoted with the `pi_…`
   id at `invoice.finalized`/`invoice.paid` (expand `payments` or read
   `confirmation_secret`). The handler must 2xx `invoice.created` quickly —
   a failing endpoint delays all automatic-collection finalization up to 72 h.
   `invoice.upcoming` cannot seed records: its invoice **has no id**.
3. **Status graph for core's subscription FSM**: two terminal states
   (`canceled`, `incomplete_expired`); `paused` reachable only from
   `trialing`; `unpaid` reachable only from `past_due` (policy-dependent,
   as is `past_due`→`canceled` and stay-`past_due`); `past_due`→`active` by
   paying (or marking uncollectible) the latest invoice; `active` does not
   imply all invoices paid. `customer.subscription.updated` is the
   authoritative carrier of every non-terminal transition (with
   `previous_attributes`); `created`/`deleted`/`paused`/`resumed` bracket the
   lifecycle; `trial_will_end` is advisory only. Scheduling a cancel
   (`cancel_at_period_end`) is an *update*, reversible until period end; the
   `deleted` event is the only "it actually ended" signal.
4. **Dunning is data, matching core's clockless model**: Stripe owns the
   clock (Smart Retries: N retries over 1w–2mo, default 8/2w; or ≤3 custom
   rules) and reports it as data on events — `attempt_count` on
   `invoice.payment_failed`, `next_payment_attempt` on the invoice (beware:
   for Automations users it arrives via `invoice.updated`). Core's dunning
   bookkeeping for the provider-managed shape should *mirror* these fields,
   never schedule anything. The post-retry outcome (canceled/unpaid/past_due)
   is merchant dashboard policy — core must model all three outcomes rather
   than assume one.
5. **Two pause semantics must not share one state**: `pause_collection` keeps
   `status=active` (signal: `customer.subscription.updated` only; invoices
   keep generating as draft/uncollectible/void per `behavior`) while `paused`
   status stops invoicing entirely (signals:
   `customer.subscription.paused`/`resumed`). A core model with a single
   "paused" flag would conflate service-entitlement pause with
   collection-only pause.
6. **Version fault line to encode in the plugin, not core**: pre-basil vs
   `2025-03-31.basil` webhook payloads differ in exactly the correlation
   fields (`invoice.subscription`→`parent`, `invoice.payment_intent`→
   `payments`/`confirmation_secret`, PI/Charge `invoice` removed,
   `current_period_*` moved to items). The payload shape follows the webhook
   endpoint's pinned API version, so the Stripe plugin needs a small
   version-tolerant accessor layer (read both shapes) rather than core
   caring.
7. **Checkout coexistence**: `mode=subscription` sessions yield
   `session.subscription` (and `session.invoice`) on
   `checkout.session.completed`; `session.payment_intent` is payment-mode
   only, so the existing cs_→pi_ promotion logic does not apply — the
   subscription flow's promotion is cs_ → sub_ (subscription identity) plus
   per-cycle in_/pi_ payment records. `payment_intent_data` (metadata
   stamping) is unavailable in subscription mode; `subscription_data.metadata`
   + `client_reference_id` are the correlation inputs instead.

## Sources

All fetched 2026-07-10, mostly via the `.md` variants of these URLs:

- [How subscriptions work (overview)](https://docs.stripe.com/billing/subscriptions/overview)
- [Subscription webhooks guide](https://docs.stripe.com/billing/subscriptions/webhooks)
- [Build a subscriptions integration](https://docs.stripe.com/billing/subscriptions/build-subscriptions)
- [Subscription invoices](https://docs.stripe.com/billing/invoices/subscription)
- [Subscription object](https://docs.stripe.com/api/subscriptions/object)
- [Create a subscription](https://docs.stripe.com/api/subscriptions/create)
- [Resume a subscription](https://docs.stripe.com/api/subscriptions/resume)
- [Invoice object](https://docs.stripe.com/api/invoices/object)
- [PaymentIntent object](https://docs.stripe.com/api/payment_intents/object)
- [Checkout Session object](https://docs.stripe.com/api/checkout/sessions/object) / [Create a Session](https://docs.stripe.com/api/checkout/sessions/create)
- [Types of events](https://docs.stripe.com/api/events/types)
- [Metadata](https://docs.stripe.com/metadata)
- [Trials](https://docs.stripe.com/billing/subscriptions/trials)
- [Pause payment collection](https://docs.stripe.com/billing/subscriptions/pause-payment)
- [Cancel subscriptions](https://docs.stripe.com/billing/subscriptions/cancel)
- [Prorations](https://docs.stripe.com/billing/subscriptions/prorations)
- [Change the price of a subscription](https://docs.stripe.com/billing/subscriptions/change-price)
- [Pending updates](https://docs.stripe.com/billing/subscriptions/pending-updates)
- [Smart Retries](https://docs.stripe.com/billing/revenue-recovery/smart-retries)
- Basil changelog: [index](https://docs.stripe.com/changelog/basil), [parent field on invoicing objects](https://docs.stripe.com/changelog/basil/2025-03-31/adds-new-parent-field-to-invoicing-objects.md), [multiple partial payments on invoices](https://docs.stripe.com/changelog/basil/2025-03-31/add-support-for-multiple-partial-payments-on-invoices.md), [item-level billing periods](https://docs.stripe.com/changelog/basil/2025-03-31/deprecate-subscription-current-period-start-and-end.md)
- [Webhooks](https://docs.stripe.com/webhooks) (ordering/dedup ground rules, via sibling asset)
