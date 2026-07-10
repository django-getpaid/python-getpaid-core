# Stripe merchant-managed recurring payments: SetupIntents, cards on file, off-session PaymentIntents

Scope note: facts needed to design **merchant-managed** recurring billing in
getpaid-core — the merchant's application runs the billing schedule and
initiates each charge against a saved payment method (card on file) via
off-session PaymentIntents. The **provider-managed** shape (Stripe Billing
runs the schedule; payments arrive unprompted) is covered by the companion
document
[`stripe-billing-subscriptions.md`](stripe-billing-subscriptions.md), and the
one-off Checkout flow (signature verification, dedup, ordering) by
[`stripe-webhooks.md`](../../../python-getpaid-stripe/docs/research/stripe-webhooks.md) —
neither is repeated here. Facts only, each claim cited to docs.stripe.com
(fetched 2026-07-10; most pages fetched as their `.md` variants). Wherever the
`2025-03-31.basil` API version changed a relevant field or shape, it is noted
inline; §7 summarizes.

## 1. SetupIntent: saving a payment method without a payment

### 1.1 What it is

Verbatim ([Setup Intents API](https://docs.stripe.com/payments/setup-intents)):
"The Setup Intents API lets you build dynamic flows for collecting payment
method details for future payments. It tracks the lifecycle of a payment setup
flow and can trigger additional authentication steps if required by law or by
the payment method." "It's similar to a payment, but no charge is created."
"The goal is to have payment credentials saved and optimized for future
payments, meaning the payment method is configured correctly for any scenario."

Object fields ([SetupIntent object](https://docs.stripe.com/api/setup_intents/object)):

- `id` (`seti_…`), `status`, `usage`, `metadata` (generic key-value pairs;
  limits per [Metadata](https://docs.stripe.com/metadata): 50 keys, 40-char
  keys, 500-char values).
- `customer` — "ID of the Customer this SetupIntent belongs to, if one exists.
  If present, the SetupIntent's payment method will be attached to the
  Customer on successful setup. Payment methods attached to other Customers
  cannot be used with this SetupIntent."
- `payment_method` — the payment method currently attached to the SetupIntent
  (nullable, expandable).
- `client_secret` — "Used for client-side retrieval using a publishable key.
  The client secret can be used to complete payment setup from your frontend.
  It should not be stored, logged, or exposed to anyone other than the
  customer."
- `next_action` — "If present, this property tells you what actions you need
  to take in order for your customer to continue payment setup."
- `last_setup_error` — "The error encountered in the previous SetupIntent
  confirmation."

### 1.2 Status lifecycle

All quotes from [Payment Intents and Setup Intents statuses](https://docs.stripe.com/payments/intents):

| Status | Verbatim |
|---|---|
| `requires_payment_method` | "After you create the SetupIntent, it has a status of `requires_payment_method` until you attach a payment method." |
| `requires_confirmation` | "After your customer provides payment information, the SetupIntent enters the `requires_confirmation` status and is ready to confirm. Most integrations skip this state because they submit payment method information when the SetupIntent is confirmed." |
| `requires_action` | "If the setup requires additional actions, such as authenticating with 3D Secure, the SetupIntent has a status of `requires_action`." |
| `processing` | "Occurs after required actions are handled. Some payment methods (for example, cards) can process quickly while other payment methods can take up to several days to process." |
| `succeeded` | "A SetupIntent with a `succeeded` status means that the setup is successful. You can now attach this payment method to a Customer object and use the payment method for future payments. If the setup fails, the SetupIntent's status returns to `requires_payment_method`." |
| `canceled` | "You can cancel a SetupIntent before it reaches a `processing` or `succeeded` state. Cancellation invalidates the SetupIntent for future setup attempts, and can't be undone." |

Note the failure edge: a failed confirmation returns the SetupIntent to
`requires_payment_method` (same page) — `succeeded` and `canceled` are the
only terminal states.

### 1.3 `usage`: `off_session` vs `on_session`

([Create a SetupIntent](https://docs.stripe.com/api/setup_intents/create)):
`usage` "Indicates how the payment method is intended to be used in the
future. If not provided, this value defaults to `off_session`." Enum values,
verbatim: `off_session` — "Use `off_session` if your customer may or may not
be in your checkout flow"; `on_session` — "Use `on_session` if you intend to
only reuse the payment method when the customer is in your checkout flow."

What it changes ([Setup Intents API](https://docs.stripe.com/payments/setup-intents)):

- "Setting `usage` to `off_session` properly authenticates a credit or debit
  card for off-session payments so that your customer doesn't have to come
  back online and re-authenticate." `off_session` "requires customer
  authentication during setup," creating "initial friction in the setup flow"
  but reducing "customer intervention in later off-session payments."
- `on_session` "lets the bank know you plan to use the card when the customer
  is available to authenticate, so you can postpone authenticating the card
  details until then and avoid upfront friction."
- "You can still use a card that's set up for on-session payments to make
  off-session payments, but banks are more likely to reject the off-session
  payment and require authentication from the customer."
- "Either case might still require later authentication, so build a recovery
  process in your app. When an off-session card payment requires
  authentication, bring your customer back online to complete the payment."

For a merchant-managed recurring flow the relevant value is therefore the
default, `off_session`.

### 1.4 Mandates and `mandate_data`

The agreement concept ([Setup Intents API](https://docs.stripe.com/payments/setup-intents)):
"If you set up a payment method for future *off-session* payments, you need
permission. Creating an agreement (sometimes called a *mandate*) up front
allows you to charge the customer when they're not actively using your website
or app." The mandate terms must cover: the customer's permission to initiate a
payment or series of payments, the anticipated frequency of payments (one-time
or recurring), and how the payment amount will be determined.

API plumbing ([Create a SetupIntent](https://docs.stripe.com/api/setup_intents/create)):
`mandate_data` — "This hash contains details about the mandate to create. This
parameter can only be used with `confirm=true`." Structure:
`customer_acceptance` (required) with `type` ∈ `online` / `offline`,
`accepted_at` (timestamp, optional), and for `online` the required
`ip_address` and `user_agent`. The same `mandate_data` hash exists on
[Create a PaymentIntent](https://docs.stripe.com/api/payment_intents/create)
(also `confirm=true`-only), alongside a `mandate` parameter — "ID of the
mandate that's used for this payment."

`single_use` on SetupIntent creation generates a single-use mandate but "are
only valid for the following payment methods: `acss_debit`, `alipay`,
`au_becs_debit`, `bacs_debit`, `bancontact`, `boleto`, `ideal`, `link`,
`sepa_debit`, and `us_bank_account`" — i.e. not plain cards
([Create a SetupIntent](https://docs.stripe.com/api/setup_intents/create)).

The Mandate object ([Mandate object](https://docs.stripe.com/api/mandates/object)):
`type` ∈ `single_use` ("Represents a one-time permission given for a single
payment") / `multi_use` ("Represents permission given for multiple payments");
`status` ∈ `active` ("The mandate can be used to initiate a payment"),
`inactive` ("The mandate was rejected, revoked, or previously used, and may
not be used to initiate future payments"), `pending`; `customer_acceptance`
("Details about the customer's acceptance of the mandate"); `payment_method`.
`payment_method_details.card` is documented ("If this mandate associates with
a `card` payment method, this hash contains mandate information specific to
the `card` payment method"), so card mandates exist as objects, but for plain
card-on-file flows the guides treat the mandate primarily as a
merchant-recorded agreement (terms + consent + record — see §3), not as an
object the integration must manage.

### 1.5 SCA: authentication happens at save time

([Strong Customer Authentication](https://docs.stripe.com/strong-customer-authentication)):
"SCA regulation requires that you authenticate your customer up front if you
intend to collect payments from them again in the future. The cardholder's
bank might decline future payments and ask for additional authentication if
the customer never authenticated initially" (also quoted verbatim in
[save-during-payment](https://docs.stripe.com/payments/save-during-payment)
and [Checkout save-and-reuse](https://docs.stripe.com/payments/checkout/save-and-reuse)).
Implementation guidance, verbatim: "For off-session payments, set up and
authenticate the card when saving the payment method"; "When saving cards
during a payment, set `setup_future_usage` to `off_session`. When saving cards
without a payment, use the Setup Intents API and set `usage` to
`off_session`." And the ceiling: "Exemptions aren't guaranteed, and
off-session payments might still require authentication by the bank."

### 1.6 Two setup paths: Checkout `mode=setup` vs SetupIntent + Elements

**Checkout Session `mode=setup`** ([Set up future payments — Checkout](https://docs.stripe.com/payments/checkout/save-and-reuse)):
"To collect customer payment details that you can reuse later, use Checkout's
setup mode. Setup mode uses the Setup Intents API to create Payment Methods."
Creation: `mode=setup`, optional `customer` (attaches the created payment
method to that customer), `success_url` (append `{CHECKOUT_SESSION_ID}`).
`currency` is "Required in `setup` mode when `payment_method_types` is not
set" ([Create a Session](https://docs.stripe.com/api/checkout/sessions/create)).
The completed Session's `setup_intent` field carries the `seti_…` id — "The ID
of the SetupIntent for Checkout Sessions in `setup` mode. You can't confirm or
cancel the SetupIntent for a Checkout Session. To cancel, expire the Checkout
Session instead" ([Session object](https://docs.stripe.com/api/checkout/sessions/object)).
Retrieval after completion is the same two routes as payment mode:
asynchronously via the `checkout.session.completed` webhook (whose
`data.object` includes `"mode": "setup"` and `"setup_intent": "seti_…"`) or
synchronously via the session id on the `success_url`
([update-payment-details guide](https://docs.stripe.com/payments/checkout/subscriptions/update-payment-details)).
The retrieved SetupIntent "contains a `payment_method` ID"
([Checkout save-and-reuse](https://docs.stripe.com/payments/checkout/save-and-reuse)).
Metadata can be stamped onto the created SetupIntent at session creation via
[`setup_intent_data.metadata`](https://docs.stripe.com/api/checkout/sessions/create#create_checkout_session-setup_intent_data-metadata) —
Stripe's own example passes an existing correlation id:
`-d "setup_intent_data[metadata][subscription_id]"="sub_…"`
([update-payment-details guide](https://docs.stripe.com/payments/checkout/subscriptions/update-payment-details)).
`customer_creation` (`always` / `if_required`) "Can only be set in `payment`
and `setup` mode"
([Create a Session](https://docs.stripe.com/api/checkout/sessions/create)).

**Raw SetupIntent + Elements** ([Save-and-reuse, Elements variant](https://docs.stripe.com/payments/save-and-reuse?payment-ui=elements)):
server creates `POST /v1/setup_intents` with `customer` and
`automatic_payment_methods[enabled]=true` (or explicit
`payment_method_types[]`); client mounts the Payment Element with the
`client_secret` and calls `stripe.confirmSetup({elements, confirmParams:
{return_url}})`. After redirect, the return URL receives `setup_intent` and
`setup_intent_client_secret` query parameters for status display. "Successful
confirmation of the `SetupIntent` saves the resulting `PaymentMethod` ID (in
`result.setupIntent.payment_method`) to the provided … `Customer`." "When
confirming the SetupIntent, Stripe.js automatically controls setting
`allow_redisplay` on the PaymentMethod, depending on whether the customer
checked the box to save their payment details."

Both paths converge: a `succeeded` SetupIntent whose PaymentMethod is attached
to the Customer, ready for §4.

## 2. Customer and payment-method storage

### 2.1 Customer as the container; attach semantics

Attaching happens automatically on successful setup when the SetupIntent (or
PaymentIntent with `setup_future_usage`) carries a `customer` (§1.1, §6.2).
The bare attach endpoint exists but is explicitly discouraged for this use
([Attach a PaymentMethod](https://docs.stripe.com/api/payment_methods/attach)),
verbatim: "We recommend you use a SetupIntent or a PaymentIntent with
`setup_future_usage`. These approaches will perform any necessary steps to set
up the PaymentMethod for future payments. Using the
`/v1/payment_methods/:id/attach` endpoint without first using a SetupIntent or
PaymentIntent with `setup_future_usage` does not optimize the PaymentMethod
for future use, which makes later declines and payment friction more likely."
This is the compliance/authentication difference: only the intent-based paths
perform the up-front SCA authentication (§1.5) that lets later off-session
charges be marked as MITs (§3).

### 2.2 Default payment method

([Customer object](https://docs.stripe.com/api/customers/object)):
`invoice_settings.default_payment_method` — "ID of a payment method that's
attached to the customer, to be used as the customer's default payment method
for subscriptions and invoices." `default_source` — "ID of the default payment
source for the customer. If you use payment methods created through the
PaymentMethods API, see the `invoice_settings.default_payment_method` field
instead" (i.e. `default_source` is the legacy Sources/Cards-era field; the
PaymentMethods-era default lives under `invoice_settings`). The attach page
confirms the setter: "To use an attached PaymentMethod as the default for
invoice or subscription payments, set `invoice_settings.default_payment_method`
on the Customer to the PaymentMethod's ID"
([Attach a PaymentMethod](https://docs.stripe.com/api/payment_methods/attach)).
Note both defaults are consumed by Stripe's *invoice/subscription* machinery;
a merchant-managed flow that passes an explicit `payment_method` on each
PaymentIntent (§4.1) does not depend on them, but one fallback exists: on
`POST /v1/payment_intents` "If you omit this parameter with `confirm=true`,
`customer.default_source` attaches as this PaymentIntent's payment instrument
to improve migration for users of the Charges API. We recommend that you
explicitly provide the `payment_method` moving forward"
([Create a PaymentIntent](https://docs.stripe.com/api/payment_intents/create)).

### 2.3 Listing and detaching

- List: `GET /v1/customers/{{CUSTOMER_ID}}/payment_methods` — "Returns a list
  of PaymentMethods for a given Customer", with optional `type` filter and an
  `allow_redisplay` filter (`always` / `limited` / `unspecified`)
  ([List a Customer's PaymentMethods](https://docs.stripe.com/api/payment_methods/customer_list)).
  The guides equivalently show `GET /v1/payment_methods?customer=…&type=card`
  ([Save-and-reuse, Elements variant](https://docs.stripe.com/payments/save-and-reuse?payment-ui=elements)).
- Detach: "Detaches a PaymentMethod object from a Customer. After a
  PaymentMethod is detached, it can no longer be used for a payment or
  re-attached to a Customer"
  ([Detach a PaymentMethod](https://docs.stripe.com/api/payment_methods/detach)) —
  detach is destructive for reuse; re-saving requires a fresh setup flow.

### 2.4 Card expiry and network updates

([CITs and MITs](https://docs.stripe.com/payments/cits-and-mits)):
"Stripe's Card Account Updater automatically updates saved cards when
necessary, such as when a card expires or is re-issued." The webhook signal is
`payment_method.automatically_updated` — "Occurs whenever a payment method's
details are automatically updated by the network"
([Types of events](https://docs.stripe.com/api/events/types)); the CIT/MIT
page's documented detection method is to compare `previous_attributes.brand`
against the current `card.brand` on that event. Brand-change caveat, verbatim:
"When a card's brand changes, you must prompt the cardholder to update their
payment method" and "When a card's brand changes, you can't charge it for any
MITs until you get a new cardholder agreement."

## 3. CIT vs MIT

Definitions ([CITs and MITs](https://docs.stripe.com/payments/cits-and-mits)),
verbatim:

- MIT: "An MIT is a transaction that you initiate without direct participation
  of your customer, based on a prior agreement with that customer authorizing
  you to store and use their credentials. For example, you operate a
  subscription-based business and your customer has consented to you
  collecting their future monthly payments using their credit card that you
  have on file."
- CIT: "CITs normally include all other transaction types, including any
  transaction where the cardholder is available to participate in the payment
  flow."
- Network context: "Card networks assign different characteristics and
  requirements to transactions, depending on whether they're customer-initiated
  or merchant-initiated. For example, a Visa transaction's authorization
  validity period varies depending on its type" (readable per charge via
  `payment_method_details.card.capture_before`).

How Stripe marks it ([Setup Intents API](https://docs.stripe.com/payments/setup-intents);
[SCA](https://docs.stripe.com/strong-customer-authentication)): "When you set
up your integration to properly save a card, Stripe marks any subsequent
off-session payment as a *merchant-initiated transaction* (A payment made
off-session with a properly authenticated saved card, can qualify as
merchant-initiated transaction and be exempt from SCA) (MIT) so that your
customers don't have to come back online and authenticate.
**Merchant-initiated transactions require an agreement between you and your
customer.**" The integration-level signals are exactly the ones from §1:
`usage=off_session` on the SetupIntent (save without payment) or
`setup_future_usage=off_session` on the PaymentIntent (save during payment),
plus `off_session=true` on each later charge (§4.1). Stripe's docs expose no
network-transaction-ID parameter for this flow — the exemption request is
described as automatic: "If, during your checkout flow, a partner (such as a
card issuer or bank) requests authentication, Stripe requests exemptions using
customer information from a previous *on-session* transaction. If the
conditions for exemption aren't met, the `PaymentIntent` might throw an error"
([Save-and-reuse, Elements variant](https://docs.stripe.com/payments/save-and-reuse?payment-ui=elements)).
Neither the exemption nor its success is guaranteed: MITs "can qualify" for
SCA exemption; "Exemptions aren't guaranteed, and off-session payments might
still require authentication by the bank"
([SCA](https://docs.stripe.com/strong-customer-authentication)).

Merchant compliance obligations for the stored credential
([CITs and MITs](https://docs.stripe.com/payments/cits-and-mits);
[save-during-payment](https://docs.stripe.com/payments/save-during-payment)):
"Include terms on your website or app that state how you save payment method
details, and require customers to opt in before you save their payment
information"; "When you save a payment method, you can only use it for the
specific purposes included in your terms"; the terms must include the
customer's agreement to a payment or series of payments, the anticipated
timing and frequency, how the amount is determined, and the cancellation
policy; "Keep a record of your customer's written agreement to your terms."

## 4. Off-session charge creation and failure modes

### 4.1 The charge call

([Save-and-reuse, Elements variant](https://docs.stripe.com/payments/save-and-reuse?payment-ui=elements);
identical shape in the [Checkout variant](https://docs.stripe.com/payments/checkout/save-and-reuse)):

```
POST /v1/payment_intents
  amount=1099 currency=usd
  customer={{CUSTOMER_ID}}
  payment_method={{PAYMENT_METHOD_ID}}
  off_session=true
  confirm=true
```

Verbatim parameter semantics:

- `off_session` — "Set to `true` to indicate that the customer isn't in your
  checkout flow during this payment attempt and can't authenticate. Use this
  parameter in scenarios where you collect payment method details and charge
  them later. This parameter can only be used with `confirm=true`"
  ([Create a PaymentIntent](https://docs.stripe.com/api/payment_intents/create)).
  On the [Confirm endpoint](https://docs.stripe.com/api/payment_intents/confirm)
  the parameter is typed `boolean | string`, but the reference text documents
  only `true`.
- `confirm` — "Set to `true` to attempt to confirm this PaymentIntent
  immediately. This parameter defaults to `false`"
  ([Create a PaymentIntent](https://docs.stripe.com/api/payment_intents/create)).
- `customer` — "Payment methods attached to other Customers cannot be used
  with this PaymentIntent" (same page); `payment_method` — "If the payment
  method is attached to a Customer, you must also provide the ID of that
  Customer as the `customer` parameter" (same page).
- `metadata` — merchant-set at creation, the generic key-value hash (same
  page). Unlike Billing's invoice-spawned PIs, nothing else writes it — the
  merchant fully controls PI metadata in this flow.
- `error_on_requires_action` — "Set to `true` to fail the payment attempt if
  the PaymentIntent transitions into `requires_action`. Use this parameter for
  simpler integrations that don't handle customer actions" (same page) — an
  opt-in to convert the 3DS-demanded case into a plain synchronous failure.

### 4.2 Synchronous outcome and failure modes

Confirm semantics ([Confirm a PaymentIntent](https://docs.stripe.com/api/payment_intents/confirm)),
verbatim: "If the selected payment method requires additional authentication
steps, the PaymentIntent will transition to the `requires_action` status and
suggest additional actions via `next_action`. If payment fails, the
PaymentIntent transitions to the `requires_payment_method` status or the
`canceled` status if the confirmation limit is reached. If payment succeeds,
the PaymentIntent will transition to the `succeeded` status (or
`requires_capture`, if `capture_method` is set to `manual`)."

For the off-session case specifically
([Save-and-reuse](https://docs.stripe.com/payments/save-and-reuse?payment-ui=elements)):
"When a payment attempt fails, the request also fails with a 402 HTTP status
code and the status of the PaymentIntent is `requires_payment_method` … You
must notify your customer to return to your application to complete the
payment (for example, by sending an email or in-app notification)."

The raised error is a `card_error`
([Errors](https://docs.stripe.com/api/errors)) carrying, verbatim: `code`
("For some errors that could be handled programmatically, a short string
indicating the error code reported"), `decline_code` ("For card errors
resulting from a card issuer decline, a short string indicating the card
issuer's reason for the decline if they provide one"), `charge` ("For card
errors, the ID of the failed charge"), and — crucially for recovery — the full
embedded object: `payment_intent` — "The PaymentIntent object for errors
returned on a request involving a PaymentIntent" (analogously `setup_intent`
for save-step errors and `payment_method`). So the declined PI's id, `status`,
and `client_secret` are all in the error payload.

Two failure families:

1. **Hard/soft card declines** — `card_declined` with a `decline_code` such as
   `expired_card` ("The card has expired. Next steps: The customer needs to
   use another card"), `insufficient_funds` ("The card has insufficient funds
   to complete the purchase. Next steps: The customer needs to use an
   alternative payment method"), `generic_decline`, `do_not_honor`
   ([Decline codes](https://docs.stripe.com/declines/codes)). Retry guidance
   is per-code: some codes say "Attempt the payment again" (`processing_error`,
   `issuer_not_available`, …); `expired_card`/`insufficient_funds` direct to a
   different payment method (same page). The recovery for non-authentication
   failures reuses the PI: "If the payment failed for other reasons, such as
   insufficient funds, send your customer to a payment page to enter a new
   payment method. You can reuse the existing PaymentIntent to attempt the
   payment again with the new payment details"
   ([Save-and-reuse](https://docs.stripe.com/payments/save-and-reuse?payment-ui=elements)).
2. **`authentication_required`** — the MIT exemption was not honored:
   "The card was declined because the transaction requires authentication such
   as 3D Secure. … In some cases, such as off-session payments, you might need
   to request the customer to retry. If the card issuer returns this decline
   code despite a successfully authenticated transaction, the customer needs
   to contact their card issuer for more information"
   ([Decline codes](https://docs.stripe.com/declines/codes)). Documented
   recovery, verbatim: "Check the code of the error raised by the Stripe API
   library. If the payment failed due to an `authentication_required` decline
   code, use the declined PaymentIntent's client secret with `confirmPayment`
   to allow the customer to authenticate the payment"
   ([Save-and-reuse](https://docs.stripe.com/payments/save-and-reuse?payment-ui=elements)) —
   i.e. bring the customer back on-session and complete 3DS on the **same**
   saved PaymentIntent, not a new one. The Checkout-based variant's
   alternative is to "direct your customer to a new Checkout Session to select
   another payment method"
   ([Checkout save-and-reuse](https://docs.stripe.com/payments/checkout/save-and-reuse)).

Test cards for exactly these paths (setup succeeds / setup requires auth /
every charge requires auth / setup declined) are documented in the same guide
([Save-and-reuse](https://docs.stripe.com/payments/save-and-reuse?payment-ui=elements)).

### 4.3 Idempotency

([Idempotent requests](https://docs.stripe.com/api/idempotent_requests)):
send an `Idempotency-Key` header on the create-and-confirm call. "Stripe's
idempotency works by saving the resulting status code and body of the first
request made for any given idempotency key, regardless of whether it succeeds
or fails. Subsequent requests with the same key return the same result,
including `500` errors." Keys are "up to 255 characters long"; "we suggest
using V4 UUIDs, or another random string with enough entropy to avoid
collisions"; keys are eligible for pruning "after they're at least 24 hours
old. We generate a new request if a key is reused after the original is
pruned"; "The idempotency layer compares incoming parameters to those of the
original request and errors if they're not the same"; "All `POST` requests
accept idempotency keys." For a billing scheduler this is the documented
mechanism to make "charge period N of agreement X" safe against duplicate
execution — within the 24-hour window.

### 4.4 Stripe does not retry bare PaymentIntents

Automatic retries are a Stripe **Billing** feature scoped to invoices and
subscriptions: "Stripe Billing can automatically retry failed subscription and
invoice payments for you"; "Using AI, Smart Retries chooses the best times to
retry failed payment attempts to increase the chance of successfully paying an
invoice"; configuration lives under **Billing > Revenue recovery > Retries**
([Smart Retries](https://docs.stripe.com/billing/revenue-recovery/smart-retries)).
No Stripe documentation describes any automatic retry of a standalone
PaymentIntent; the off-session guides put the follow-up entirely on the
merchant ("You must notify your customer to return to your application…",
§4.2), and the intent lifecycle page frames retry as a merchant action ("the
PaymentIntent's status returns to `requires_payment_method` so that the
payment can be retried" —
[Intent statuses](https://docs.stripe.com/payments/intents)). Retry
scheduling for merchant-managed recurring is therefore the merchant's job, per
decline-code guidance (§4.2).

## 5. Webhook traffic from the save step and from MIT charges

Event descriptions verbatim from
[Types of events](https://docs.stripe.com/api/events/types). Delivery ground
rules (signature verification, dedup by `event.id`, no ordering guarantee) are
covered in the one-off research (`stripe-webhooks.md`) and apply unchanged.

### 5.1 Save step (`data.object` = SetupIntent / PaymentMethod)

| Event | Fires when |
|---|---|
| `setup_intent.created` | "Occurs when a new SetupIntent is created." |
| `setup_intent.requires_action` | "Occurs when a SetupIntent is in requires_action state." |
| `setup_intent.succeeded` | "Occurs when an SetupIntent has successfully setup a payment method." — the save-complete signal |
| `setup_intent.setup_failed` | "Occurs when a SetupIntent has failed the attempt to setup a payment method." (details in `last_setup_error`, §1.1) |
| `setup_intent.canceled` | "Occurs when a SetupIntent is canceled." |
| `payment_method.attached` | "Occurs whenever a new payment method is attached to a customer." |
| `payment_method.detached` | "Occurs whenever a payment method is detached from a customer." |
| `payment_method.updated` | "Occurs whenever a payment method is updated via the PaymentMethod update API." |
| `payment_method.automatically_updated` | "Occurs whenever a payment method's details are automatically updated by the network." (§2.4) |
| `mandate.updated` | "Occurs whenever a Mandate is updated." |
| `checkout.session.completed` | for the Checkout `mode=setup` path; `data.object` is the Session with `mode: "setup"` and `setup_intent: "seti_…"` (§1.6) |

Correlation fields on the SetupIntent payload: `customer`, `payment_method`,
and merchant-set `metadata` (settable directly at SetupIntent creation, or via
`setup_intent_data.metadata` on a setup-mode Checkout Session — §1.6).

### 5.2 Charge step (`data.object` = PaymentIntent / Charge)

An off-session create-and-confirm produces the normal PaymentIntent stream —
there is no invoice or subscription object involved and no extra event types:

| Event | Fires when |
|---|---|
| `payment_intent.created` | "Occurs when a new PaymentIntent is created." |
| `payment_intent.processing` | "Occurs when a PaymentIntent has started processing." |
| `payment_intent.requires_action` | "Occurs when a PaymentIntent transitions to requires_action state" (3DS demanded — the async twin of the `authentication_required` error, §4.2) |
| `payment_intent.succeeded` | "Occurs when a PaymentIntent has successfully completed payment." |
| `payment_intent.payment_failed` | "Occurs when a PaymentIntent has failed the attempt to create a payment method or a payment." |
| `charge.succeeded` / `charge.failed` | "Occurs whenever a charge is successful." / "Occurs whenever a failed charge attempt occurs." |

Correlation fields on these PI payloads: `metadata` is **merchant-set at
creation** (§4.1) — the defining difference from Billing's invoice-spawned PIs,
which carry no merchant metadata (companion doc §1.2); plus `customer`,
`payment_method`, and (on the Charge, via snapshot) "When a PaymentIntent
creates a Charge, the metadata copies to the Charge in a one-time snapshot"
([Metadata](https://docs.stripe.com/metadata)). Since the merchant's own
server created the PI, the `pi_…` id is also known before any webhook arrives
— webhook handling can be pure id-match, exactly like the existing one-off
design.

## 6. Coexistence with the one-off Checkout design

The already-specced one-off flow uses Checkout Sessions in `payment` mode with
PaymentIntent-authoritative webhook handling (PI metadata stamped via
`payment_intent_data`, correlation by PI id/metadata). Facts relevant to
adding merchant-managed recurring alongside it:

### 6.1 Session modes are disjoint and self-labeling

`mode` ∈ `payment` ("Accept one-time payments for cards, iDEAL, and more"),
`setup` ("Save payment details to charge your customers later"),
`subscription` ("Use Stripe Billing to set up fixed-price subscriptions")
([Session object](https://docs.stripe.com/api/checkout/sessions/object)).
`session.payment_intent` is populated only "for Checkout Sessions in `payment`
mode"; `session.setup_intent` only "in `setup` mode" (same page). A setup-mode
session moves no money and produces no `payment_intent.*` events of its own.

### 6.2 Charge-and-save in one session: `payment_intent_data.setup_future_usage`

A `payment`-mode session can save the card while charging it, via
[`payment_intent_data.setup_future_usage`](https://docs.stripe.com/api/checkout/sessions/create),
verbatim: "Indicates that you intend to make future payments with the payment
method collected by this Checkout Session. When setting this to `on_session`,
Checkout will show a notice to the customer that their payment details will be
saved. When setting this to `off_session`, Checkout will show a notice to the
customer that their payment details will be saved and used for future
payments. If a Customer has been provided or Checkout creates a new Customer,
Checkout will attach the payment method to the Customer. If Checkout does not
create a Customer, the payment method is not attached to a Customer."

The underlying PaymentIntent parameter
([Create a PaymentIntent](https://docs.stripe.com/api/payment_intents/create)):
`setup_future_usage` (enum `none` / `on_session` / `off_session`) — "Indicates
that you intend to make future payments with this PaymentIntent's payment
method. If you provide a Customer with the PaymentIntent, you can use this
parameter to attach the payment method to the Customer after the PaymentIntent
is confirmed and the customer completes any required actions. … When
processing card payments, Stripe uses `setup_future_usage` to help you comply
with regional legislation and network rules, such as SCA." This is the
save-during-payment counterpart of SetupIntent `usage` (§1.3, §1.5): the CIT
that both charges and authenticates the credential for later MITs.

### 6.3 Distinguishing PI populations in a shared webhook endpoint

Three PI populations can now hit one endpoint; documented discriminators:

- **One-off Checkout PIs**: born from a `payment`-mode session; merchant
  metadata present via `payment_intent_data.metadata` — "Data you include with
  the `payment_intent_data.metadata` attribute saves to the underlying
  PaymentIntent's metadata" ([Metadata](https://docs.stripe.com/metadata));
  the `pi_…` id is learned from `session.payment_intent` at
  `checkout.session.completed`.
- **Merchant-initiated off-session PIs**: created directly by the merchant's
  own server (§4.1), so both the id and the metadata are merchant-controlled
  *before* any event fires; `metadata` can carry the local
  agreement/period keys.
- **Stripe Billing invoice PIs** (if the provider-managed shape also runs):
  carry **no** merchant metadata and, on basil, no `invoice` field — the basil
  changelog entry ["Adds support for multiple (partial) payments on
  invoices"](https://docs.stripe.com/changelog/basil/2025-03-31/add-support-for-multiple-partial-payments-on-invoices.md)
  (breaking) removed `payment_intent.invoice` and `charge.invoice`.
  Consequence for coexistence: `invoice` is absent from **all three**
  populations' basil payloads (null/absent on bare and Checkout PIs because
  none exists; removed from the schema for invoice PIs), so `invoice` is not a
  discriminator on basil — pre-basil it distinguishes Billing PIs (non-null
  `in_…`). The robust rule is id/metadata match: known `pi_…` or
  merchant-stamped metadata ⇒ one-off or merchant-managed (distinguishable by
  the merchant's own metadata keys); neither ⇒ foreign traffic (companion doc
  §7.4).
- The save step is likewise separable: setup-mode sessions correlate by
  `session.setup_intent` + `setup_intent_data.metadata` (§1.6), and
  `setup_intent.*` events never carry payment amounts.

## 7. `2025-03-31.basil` changes on these surfaces

The [basil changelog](https://docs.stripe.com/changelog/basil) lists **no
changes to SetupIntents, Mandates, or Customers**, and only peripheral
PaymentMethod changes (new/reusable local payment methods: Naver Pay
save/reuse, Billie, Satispay, NZ BECS). Two entries touch this design's
surfaces:

1. `payment_intent.invoice` and `charge.invoice` were removed (breaking; part
   of ["Adds support for multiple (partial) payments on
   invoices"](https://docs.stripe.com/changelog/basil/2025-03-31/add-support-for-multiple-partial-payments-on-invoices.md)) —
   effect on discrimination in §6.3.
2. "Partially capturing or canceling payments no longer creates a Refund"
   (breaking, PaymentIntents) — only relevant if the recurring design uses
   manual capture; the flows documented here (`confirm=true`, automatic
   capture) are unaffected.

Otherwise, nothing in basil changed the SetupIntent flow, `usage`,
`mandate_data`, `setup_future_usage`, `off_session`, attach/detach, or the
event set documented above.

## Sources

All fetched 2026-07-10, mostly via the `.md` variants of these URLs:

- [Setup Intents API (guide)](https://docs.stripe.com/payments/setup-intents)
- [SetupIntent object](https://docs.stripe.com/api/setup_intents/object) / [Create a SetupIntent](https://docs.stripe.com/api/setup_intents/create)
- [Payment Intents and Setup Intents statuses](https://docs.stripe.com/payments/intents)
- [Save a customer's payment method without making a payment (Elements variant)](https://docs.stripe.com/payments/save-and-reuse?payment-ui=elements) and [Checkout Sessions API variant](https://docs.stripe.com/payments/save-and-reuse?payment-ui=embedded-components)
- [Set up future payments — Checkout hosted](https://docs.stripe.com/payments/checkout/save-and-reuse)
- [Save payment details during a payment](https://docs.stripe.com/payments/save-during-payment)
- [Update payment details (setup mode + setup_intent_data example)](https://docs.stripe.com/payments/checkout/subscriptions/update-payment-details)
- [CITs and MITs](https://docs.stripe.com/payments/cits-and-mits)
- [Strong Customer Authentication](https://docs.stripe.com/strong-customer-authentication)
- [Create a PaymentIntent](https://docs.stripe.com/api/payment_intents/create) / [Confirm a PaymentIntent](https://docs.stripe.com/api/payment_intents/confirm)
- [Attach a PaymentMethod](https://docs.stripe.com/api/payment_methods/attach) / [Detach a PaymentMethod](https://docs.stripe.com/api/payment_methods/detach) / [List a Customer's PaymentMethods](https://docs.stripe.com/api/payment_methods/customer_list)
- [Customer object](https://docs.stripe.com/api/customers/object)
- [Mandate object](https://docs.stripe.com/api/mandates/object)
- [Declines](https://docs.stripe.com/declines) / [Decline codes](https://docs.stripe.com/declines/codes)
- [Errors](https://docs.stripe.com/api/errors)
- [Idempotent requests](https://docs.stripe.com/api/idempotent_requests)
- [Smart Retries](https://docs.stripe.com/billing/revenue-recovery/smart-retries)
- [Types of events](https://docs.stripe.com/api/events/types)
- [Metadata](https://docs.stripe.com/metadata)
- [Create a Session](https://docs.stripe.com/api/checkout/sessions/create) / [Session object](https://docs.stripe.com/api/checkout/sessions/object)
- Basil changelog: [index](https://docs.stripe.com/changelog/basil), [multiple partial payments on invoices](https://docs.stripe.com/changelog/basil/2025-03-31/add-support-for-multiple-partial-payments-on-invoices.md)
