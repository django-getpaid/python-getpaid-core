# PayU recurring card payments: token model and CIT/MIT semantics

Scope note: facts needed to design **merchant-managed** card-on-file recurring
billing in getpaid-core, documented against PayU GPO Europe (the
Polish/CEE REST API on `secure.payu.com`, API v2.1) — the merchant's
application runs the billing schedule and initiates each charge against a
stored card token. The Stripe shapes are in the companion documents
[`stripe-merchant-managed-recurring.md`](stripe-merchant-managed-recurring.md)
(merchant-managed) and
[`stripe-billing-subscriptions.md`](stripe-billing-subscriptions.md)
(provider-managed) — PayU has no provider-managed equivalent (§2.5). Facts
only, each claim cited to developers.payu.com (fetched 2026-07-10; API-reference
claims are quoted from the OpenAPI spec the reference publishes at
[`/europe/resources/payu-api-ref.yaml`](https://developers.payu.com/europe/resources/payu-api-ref.yaml),
rendered at [PayU GPO Europe REST API](https://developers.payu.com/europe/api/)).

## 1. Token model

### 1.1 Single-use (`TOK_`) vs multi-use (`TOKC_`) tokens

([Creating Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/create-token/)):
two scenarios for a single-use token, verbatim: "a one-time payment with a
single-use token — use the tokenize method with `SINGLE` passed as its value.
This will enable you to tokenize the card information and receive a single-use
token that can be used for one payment only"; "the first payment with saving
the card for future use — use the tokenize method with `MULTI` passed as its
value. This will enable you to tokenize the card information and receive a
single-use token that can be used for a one-time payment, and saved for future
use." The `tokenize` method belongs to the front-end
[Secure Form](https://developers.payu.com/europe/docs/checkout/secure-form/)
widget — card entry never touches the merchant server.

"A multi-use token (TOKC_) is created after the first use of a single-use
token (TOK_), Google Pay Token, or Apple Pay Token"
([Creating Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/create-token/)).
There is no endpoint that mints a `TOKC_` directly: the durable credential is
always the by-product of an **order** (a payment or a zero-amount order, §1.4)
that carried the single-use token.

### 1.2 Two parameter families: `recurring` and `cardOnFile`

Both live at the top level of OrderCreateRequest and are **mutually
exclusive**. Verbatim from the
[API Reference — Create an Order](https://developers.payu.com/europe/api/#tag/Order/operation/create-an-order):

- `cardOnFile` — "Information about party initializing order or a transaction
  with Buyer consent to save card token. 'cardOnFile' parameter cannot be used
  with 'recurring' parameter." Enum: `FIRST`, `STANDARD_CARDHOLDER`,
  `STANDARD_MERCHANT`.
- `recurring` — "Marks the order as recurring payment: **FIRST** — payment
  initialized by the card owner who agreed to save card for future use in
  recurring plan. You can expect full authentication (3D Secure and/or CVV).
  If you want to use multi-use token (TOKC_) later, you have to be confident,
  that first recurring payment was successful. **STANDARD** — subsequent
  recurring payment (user is not present). This transaction has multi use
  token (TOKC_). You cannot use it if FIRST recurring payment failed.
  `recurring` parameter cannot be used with `cardOnFile` parameter."

The semantic split between the families
([Charging Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/charge-token/), §2.1):
`cardOnFile` distinguishes CIT (`STANDARD_CARDHOLDER`) from MIT
(`STANDARD_MERCHANT`) for ad-hoc card-on-file use, while `recurring=STANDARD`
is the MIT flavor specific to "a subsequent payment in the cycle." The
[Creating Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/create-token/)
page treats them as parallel at save time: "`cardOnFile` / `recurring`
parameter should be set to `FIRST`." No precedence rule is documented because
they can never co-occur. A guidance note:
"Carefully considering the value of the `cardOnFile` and `recurring` parameter
can increase conversion for payment cards"
([Charging Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/charge-token/)).

### 1.3 Creating the multi-use token during a purchase (the CIT FIRST payment)

([Creating Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/create-token/)),
verbatim requirements: "When creating a new order, you should extend it with
`buyer` and `payMethod` sections where single-use token (TOK_) is used as a
`value` parameter. Additionally, `cardOnFile` parameter should be set to
`FIRST` … Make sure that your point of sale (POS) is properly configured to
allow the creation of multi-use tokens. In the `buyer` section, you should
send the `extCustomerId` parameter with the customer's identifier from your
system. This parameter is used to retrieve the payment methods saved by the
customer."

Request shape (`POST /api/v2_1/orders`, same page's example): normal order
fields (`notifyUrl`, `customerIp`, `merchantPosId`, `currencyCode`,
`totalAmount`, `extOrderId`, `products`) plus `cardOnFile: "FIRST"`,
`buyer.extCustomerId`, and
`payMethods.payMethod = {"value": "TOK_…", "type": "CARD_TOKEN"}`.
The [Recurring Payments](https://developers.payu.com/europe/docs/payment-solutions/cards/recurring/)
variant is identical except `recurring: "FIRST"` and a
`threeDsAuthentication.recurring` object (`frequency`, `expiry` — §5.1);
notably its FIRST/STANDARD examples send `buyer` without `extCustomerId`
(email, name, language only) — `extCustomerId` is tied to token *retrieval*
(§1.6), not to charging.

Wallet-funded tokens: the first payment can also be made with a Google Pay or
Apple Pay token passed Base64-encoded as `payMethod.authorizationCode`
([Recurring Payments](https://developers.payu.com/europe/docs/payment-solutions/cards/recurring/)).
Restriction, verbatim: "If the transaction was conducted using Google Pay or
Apple Pay, the obtained token can be used for merchant-initiated transactions
(`cardOnFile: STANDARD_MERCHANT`). However, the created token cannot be used
for customer-initiated transactions (`cardOnFile: STANDARD_CARDHOLDER`)"
([Creating Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/create-token/));
`STANDARD_CARDHOLDER` "is not available for transactions created using Google
Pay or Apple Pay"
([Charging Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/charge-token/)).

Prerequisite for the whole feature, verbatim: "Before you begin integrating
the recurring payments service, make sure to contact PayU via your account
manager or our contact form for necessary configuration operations. On the
Sandbox environment, REST API points of sale will be configured automatically,
and you'll be able to test this functionality within 90 minutes of creation"
([Recurring Payments](https://developers.payu.com/europe/docs/payment-solutions/cards/recurring/)).

### 1.4 Save-without-payment: the zero-amount order

PayU's closest analogue to a Stripe SetupIntent is a **zero-amount order**
([Creating Tokens — Creating Multi-use Tokens Without Purchase](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/create-token/)),
verbatim conditions:

- "In the request body, the `totalAmount` parameter should be set to 0 …
  even though the `totalAmount` is set to 0, you still need to provide the
  currency parameter for the sake of API consistency."
- "`products` object is not required."
- "`cardOnFile` / `recurring` parameter should be set to `FIRST`."
- "Zero-amount orders are always auto-received … for zero-amount always expect
  status transition from `PENDING` to either `COMPLETED` or `CANCELED` but
  never to `WAITING_FOR_CONFIRMATION`."
- "Zero-amount orders are only possible for card payments."
- "Zero-amount orders are not possible for 'shops' configured as marketplace."
- Gated feature: "Special arrangements are required before this feature is
  enabled in either sandbox or production enviroment. Please contact your
  sales representative in PayU first."

Compliance rationale, verbatim: "When a card is stored without making an
actual purchase, it is essential to avoid creating an order with a non-zero
total amount that is later canceled. Such approach is not compliant with card
scheme regulations."

### 1.5 Where the multi-use token is returned

The `TOKC_` value appears **synchronously in the OrderCreateResponse** of the
FIRST order: the documented response (both the purchase and zero-amount cases)
carries `payMethods.payMethod` with `type: "CARD_TOKEN"`,
`value: "TOKC_…"`, and a `card` sub-object with masked `number`,
`expirationMonth`, `expirationYear` — "Highlighted in the response is the
multi-use token (TOKC_), which can be used for future payments"
([Creating Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/create-token/)).
In the API reference this `payMethods` node on the 3DS-redirect response
variant is "Optional. Contains card token details if it was created"
([API Reference — Create an Order](https://developers.payu.com/europe/api/#tag/Order/operation/create-an-order)).

But the docs direct merchants **not** to persist it from there, verbatim:
"For multi-use tokens (TOKC_), you should always retrieve them from PayU,
instead of copying them from the order response. Retrieving tokens from PayU
ensures that you have access to additional information such as token
expiration date, token status, and more"
([Creating Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/create-token/)).
Stronger still: "payment methods available for a user should not be stored
locally on the merchant's server. Instead, they should be retrieved from the
PayU system for each payment"
([Retrieving and Deleting tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/retrieve-and-delete-token/)).
The retrieval channel is `GET /api/v2_1/paymethods` (§3.1). The token value
does **not** appear in order status notifications (§6.2).

### 1.6 Token scoping and the `trusted_merchant` OAuth grant

Token retrieval and deletion require an OAuth token of a dedicated grant type
([Retrieving and Deleting tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/retrieve-and-delete-token/)),
verbatim: "To retrieve payment methods and tokens, you must first obtain an
OAuth access token with the type of `trusted_merchant`. To create a token, you
should use the customer's data (email address, `ext_customer_id`) for whom you
want to retrieve payment methods." The grant is defined in the
[API Reference — Authorize](https://developers.payu.com/europe/api/#tag/Authorize):
"`trusted_merchant` — used for authentication of requests made for logged-in
shop/application users with fixed `extCustomerId`"; its required fields are
`grant_type`, `client_id` ("Merchant's POS identifier in PayU's system"),
`client_secret`, `email` ("Customer's email address in merchant's system"),
and `ext_customer_id` ("Customer's identifier in the merchant's system").
Access tokens are "valid for 43199 seconds" (same page).

A stored token is therefore addressed by the triple (POS credentials, buyer
email, `ext_customer_id`); the docs do not document any cross-POS or
cross-merchant token use, nor any token-portability mechanism. Regular charge
calls (`POST /api/v2_1/orders`) accept either `client_credentials` or
`trusted_merchant` bearer tokens
([API Reference — Create an Order](https://developers.payu.com/europe/api/#tag/Order/operation/create-an-order),
`security` on the operation).

## 2. Subsequent MIT charges

### 2.1 The charge call

A subsequent charge is an ordinary OrderCreateRequest
([Charging Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/charge-token/)):
`POST /api/v2_1/orders` with `buyer`,
`payMethods.payMethod = {"value": "TOKC_…", "type": "CARD_TOKEN"}`, and one of
the initiation flags. Verbatim semantics:

- `cardOnFile: STANDARD_CARDHOLDER` — "refers to a payment made with a
  previously saved card, initiated by the card owner. Depending on specific
  payment parameters, such as high transaction amounts, strong authentication,
  such as 3D Secure and/or CVV, may be required." (CIT — one-click.)
- `cardOnFile: STANDARD_MERCHANT` — "refers to a payment made with a
  previously saved card, initiated by the shop or merchant without the
  involvement of the card owner. As per the definition, this payment type does
  not necessitate strong authentication. However, it's important to note that
  you cannot use this option if the FIRST card-on-file payment had previously
  failed." (MIT.)
- `recurring: STANDARD` — "refers to a subsequent payment in the cycle made
  with a previously saved card, initiated by the shop or merchant without the
  involvement of the card owner. As per the definition, this payment type does
  not necessitate strong authentication. However, it's important to note that
  you cannot use this option if the FIRST recurring payment had previously
  failed." (MIT, subscription-cycle flavor.)

The [Recurring Payments](https://developers.payu.com/europe/docs/payment-solutions/cards/recurring/)
page's STANDARD example also includes the `threeDsAuthentication.recurring`
object (`frequency`, `expiry`) on the subsequent charge, and frames the MIT
property, verbatim: "All transactions, except of the first one, are not
initiated by the cardholder. They can be performed by a scheduler on your
side at any time, even at night. Therefore, neither 3DS nor CVV authentication
is required."

### 2.2 Synchronous response

Possible OrderCreateResponse statuses for a token charge
([Charging Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/charge-token/);
[Recurring Payments](https://developers.payu.com/europe/docs/payment-solutions/cards/recurring/)):

| `status.statusCode` | Meaning |
|---|---|
| `SUCCESS` | Request accepted. "This response indicates that additional payer authentication may not be necessary." The response repeats `payMethods.payMethod` with the `TOKC_` token and masked card. Final payment outcome still arrives asynchronously: "PayU keeps you informed about the payment status by sending a notification to the address specified in the `notifyUrl` parameter." |
| `WARNING_CONTINUE_3DS` | "the payer should be redirected to the card issuer's page using the `redirectUri` parameter for additional payment authentication through the 3D Secure process … exclusively via the 3DS 2 protocol." After authentication the payer returns to `continueUrl` with query parameters `statusCode` (`SUCCESS` or `WARNING_CONTINUE_CVV`) and `refReqId`. |
| `WARNING_CONTINUE_CVV` | "you should ask the payer to provide CVV2/CVC2 code" — via Secure Form's `extractRefReqId` + `sendCvv` methods. |

Note the sync `SUCCESS` is only request acceptance: authorization outcome
(including a decline) is delivered via the order status notification
(`COMPLETED` / `CANCELED`, §6). An opt-in exists to get the authorization
result synchronously: `settings.syncProcessing` — "If true, order
authorization result will be returned synchronously when applicable (e.g.,
card payments without 3DS authentication, incl. Apple Pay and Google Pay).
Available upon request, requires contact with a PayU representative"
([API Reference — Create an Order](https://developers.payu.com/europe/api/#tag/Order/operation/create-an-order)).

### 2.3 Whether 3DS is skipped for MIT

Per §2.1, MIT charges (`recurring=STANDARD` / `cardOnFile=STANDARD_MERCHANT`)
"do not necessitate strong authentication" and no 3DS/CVV is required by
definition. However the charge-token page immediately hedges on the `SUCCESS`
response: "depending on specific circumstances, 3DS or CVV may still be
required during the payment process"
([Charging Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/charge-token/)) —
i.e. `WARNING_CONTINUE_3DS` / `WARNING_CONTINUE_CVV` are possible responses
even for a token charge, and issuers can soft-decline an unauthenticated
authorization (§5.3). PayU-side exemption machinery for stored cards:
"the PayU system will attempt to utilize one of the available exemptions to
avoid Strong Customer Authentication (SCA) on your behalf"
([Handling Soft Declines](https://developers.payu.com/europe/docs/card-payments/threeds/soft-declines/)).

### 2.4 Documented constraints

- **FIRST must have succeeded**: both MIT flags are unusable "if the FIRST …
  payment had previously failed" (§2.1). The API reference adds: "If you want
  to use multi-use token (TOKC_) later, you have to be confident, that first
  recurring payment was successful" (§1.2).
- **Mutual exclusivity** of `recurring` and `cardOnFile` (§1.2).
- No documented amount- or currency-matching constraint between FIRST and
  STANDARD charges; each order carries its own `totalAmount`/`currencyCode`.
  No constraint text ties a token to a currency. (Absence noted, not a
  guarantee.)
- The network-level linkage of the series is visible read-only as
  `firstTransactionId` — "Identifier of the first of recurring payments or
  Card-on-File, granted by the payment organisation" — in the
  [transaction retrieve](https://developers.payu.com/europe/api/#tag/Order/operation/retrieve-a-transaction)
  response; there is no documented request parameter for supplying it (PayU
  manages the network transaction ID chain itself).
- Order retrieve/transaction data classify the flow via `payment_flow` enum
  values such as `ONE_CLICK_CARD`, `ONE_CLICK_CARD_RECURRING`,
  `ONE_CLICK_MAIL_RECURRING`, `ONE_CLICK_PHONE_RECURRING`
  ([API Reference — Retrieve an Order](https://developers.payu.com/europe/api/#tag/Order)).

### 2.5 No provider-side schedule

The scheduling is explicitly the merchant's: subsequent payments "can be
performed by a scheduler on your side at any time"
([Recurring Payments](https://developers.payu.com/europe/docs/payment-solutions/cards/recurring/)).
No PayU GPO documentation describes a subscription/plan object, a
provider-run billing schedule, or automatic re-charging — the recurring
"service" is exactly: tokenization + initiation flags on merchant-created
orders.

## 3. Token lifecycle and expiry

### 3.1 Retrieval: `GET /api/v2_1/paymethods`

([Retrieving and Deleting tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/retrieve-and-delete-token/)):
authenticated with a `trusted_merchant` token for a specific (email,
`ext_customer_id`) pair (§1.6). Response arrays: `cardTokens` ("returned
empty if the user does not have any active or expired card tokens"),
`payByLinks` (redirect methods), and `blikTokens`. Each `cardTokens` entry
([API Reference — Retrieve Payment Methods](https://developers.payu.com/europe/api/#tag/Payment-Methods)):
`value` (`TOKC_…`), `status` — enum `NEW` / `ACTIVE` / `EXPIRED` —
`cardExpirationYear`, `cardExpirationMonth`, `cardNumberMasked`, `cardScheme`,
`cardBrand`, `brandImageUrl`, and `preferred` (boolean). The docs give no
further definition of `NEW` vs `ACTIVE`, and no statement of when a token
flips to `EXPIRED` other than the card expiry data being present; retrieval
per payment is the documented freshness mechanism ("The provided payment
methods are always up-to-date and relevant for the user at the given moment",
same page).

### 3.2 Deletion: `DELETE /api/v2_1/tokens/{tokenValue}`

Verbatim: "If the buyer terminates the user account in your shop or chooses to
remove the stored card from the user account, you must delete the token. To
delete the token, simply send a DELETE request to
`https://secure.payu.com/api/v2_1/tokens/{tokenValue}` … The header should
include a OAuth token obtained with a `grant_type=trusted_merchant`" for that
customer's (email, `ext_customer_id`)
([Retrieving and Deleting tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/retrieve-and-delete-token/)).
"This method can only be used to delete card tokens retrieved from the
`cardTokens` array" (same page). Success is HTTP 204
([API Reference — Delete a Token](https://developers.payu.com/europe/api/#tag/Token)).

### 3.3 No account updater; no documented post-failure invalidation

- No PayU GPO Europe documentation describes a card account-updater feature
  (automatic refresh of expired/reissued cards); a search of
  developers.payu.com finds none. The only documented card-refresh path is the
  buyer saving the card again through a new FIRST transaction (§1.3).
- What happens to a token after a **failed subsequent charge** is not
  documented — no statement that a token is invalidated or blocked after N
  declines. The only failure rule attached to tokens is the FIRST-payment
  rule (§2.4). A failed FIRST means the `TOKC_` (if one was issued in the
  response) must not be used for MIT flags at all (§1.2, §2.1).

## 4. Failure semantics and retry constraints

### 4.1 Where failures surface

An MIT charge that fails after a `SUCCESS` acceptance surfaces as an order
status notification with `status: CANCELED` — "Payment has been cancelled and
the buyer has not been charged (no money was taken from buyer's account)"
([Payment Lifecycle](https://developers.payu.com/europe/docs/payment-flows/lifecycle/)).
The decline *reason* is not in the notification; the documented pattern is:
"For each failed card payment, retrieve the transaction data using
transaction data retrieval"
([Handling Soft Declines](https://developers.payu.com/europe/docs/card-payments/threeds/soft-declines/)) —
`GET /api/v2_1/orders/{orderId}/transactions`, whose card section includes
`cardResponseCode`, `cardResponseCodeDesc`, `card3DsStatus`,
`card3DsStatusDescription`, `cardBinCountry`, and `firstTransactionId`
([Transaction Data Retrieval](https://developers.payu.com/europe/docs/payment-flows/transaction-retrieve/);
"After a transaction has been processed, card details become available
immediately"). Order retrieve can also be asked for authorization details
(`with=authorization`), whose `status` enum is `AUTHORIZED` /
`SOFT_DECLINED` / `REJECTED` / `PENDING` with `responseCode` /
`responseCodeDescription`
([API Reference — Retrieve an Order](https://developers.payu.com/europe/api/#tag/Order)).

### 4.2 Card status codes and per-code retry guidance

The decline code list is
[Card Status Codes](https://developers.payu.com/europe/docs/card-payments/card-status-codes/)
(columns: Card ResponseCode, description, Reason — "which side stopped the
transaction" — additional information, public communication). Rows relevant to
recurring, verbatim from the "Additional Information" column:

| Code | Description | Retry guidance |
|---|---|---|
| `000` | "000 - OK" | "Successful authorization. Funds were transferred to the recipient." |
| `S01` | "Refer to card issuer" | (bank decline; no retry note) |
| `S05` | "Do not honor" | (bank decline; no retry note) |
| `S51` | "Insufficient funds" | "Not enough funds or attempt to exceed limits (at the bank side)." |
| `S54` | "Expired card" | "Card is expired or Customer made a mistake giving card dates." |
| `SSD` | "Soft decline (strong authentication required)" | "Payment can be retried, but 3DS authentication must be used." |
| `SP1` | "Over daily limit (try again later)" | "Payment can be retried, preferrably after 24 hours." |
| `SPF` | "Possible fraud (do not try again)" | "Payment must not be retried, all further attempts will be declined." |
| `SAC` | "Account closed (do not try again)" | "Payment must not be retried, all further attempts will be declined." |

PayU does not publish card-network MIT retry ceilings (e.g. Visa's
per-window retry limits) in this documentation set; the per-code guidance
above is the only documented retry constraint.

### 4.3 PayU performs no payment retries

No PayU documentation describes PayU re-attempting a failed charge on the
merchant's behalf — consistent with the merchant-side scheduler framing
(§2.5). The only automatic retry machinery documented anywhere is
**notification redelivery** (§6.4), which retries message delivery, not
payments. Retry scheduling after declines is therefore entirely the
merchant's job, bounded by the per-code guidance (§4.2) and the "do not
retry" codes.

## 5. 3DS/SCA involvement

### 5.1 SCA at FIRST

"Each initial payment requires 3DS authentication, it is suggested to include
data required by 3DS, especially `recurring` object in the
`threeDsAuthentication` section"
([Recurring Payments](https://developers.payu.com/europe/docs/payment-solutions/cards/recurring/)).
The `threeDsAuthentication.recurring` object
([API Reference — Create an Order](https://developers.payu.com/europe/api/#tag/Order/operation/create-an-order)):

- `frequency` — "The minimum number of days between recurring payments (e.g.
  setting it to **7** for a weekly cycle). However, according to the
  recommendations of card schemes, when dealing with recurring payments that
  have a variable frequency, it's advised to use a value of **1** for this
  parameter."
- `expiry` — "Date after no further recurring payments will be performed.
  According to recommendation by the card schemes, in cases where there is no
  established expiry or end date of recurring (e.g. subscriptions), the value
  of `9999-12-31T00:00:00Z` should be used."

"While this object is not mandatory, we strongly recommend including it in
your recurring transaction request for optimal processing. Highlighted
`recurring` object is different entity than `recurring` parameter described
earlier on this page!"
([Recurring Payments](https://developers.payu.com/europe/docs/payment-solutions/cards/recurring/)).
The same page notes for wallets: "If the source of the card data is a Google
Pay Token or Apple Pay Token, there is a high probability that 3DS
authentication will not be enforced due to the security measures applied to
card data by Apple Pay and Google Pay."

The challenge flow itself is redirect-based (`WARNING_CONTINUE_3DS` +
`redirectUri`, return to `continueUrl` with `statusCode`/`refReqId` — §2.2);
`iframeAllowed` on the response "Indicates whether 3DS authentication page can
be displayed in iframe" and `threeDsProtocolVersion` is `3DS2`
([API Reference — Create an Order](https://developers.payu.com/europe/api/#tag/Order/operation/create-an-order)).

### 5.2 Merchant-requested challenge and exemptions

`threeDsAuthentication` also carries two mutually exclusive steering fields
([API Reference — Create an Order](https://developers.payu.com/europe/api/#tag/Order/operation/create-an-order)):

- `challengeRequested` — "Merchant's preference regarding 3DS 2 challenge.
  Exclusive with exemption. Maybe overridden by PayU." Enum `YES` / `NO` /
  `MANDATE`.
- `exemption` — "Merchant's preference regarding SCA exemption to be used to
  exempt card payment from 3DS authentication. Exclusive with
  challengeRequested. Requires additional configuration to be enabled." —
  `value`: "Either `LOW_RISK` (also known as TRA — transaction risk analysis
  compliant with SCA requirements has been performed by the merchant) or
  `LOW_VALUE` (low value payment, up to 30 EUR or equivalent in other
  currency)"; `rejectionHandling`: "Either `PERFORM_AUTHENTICATION` (PayU
  will return response with WARNING_CONTINUE_3DS and redirection URL code if
  exemption cannot be applied) or `DECLINE` (PayU will decline the payment if
  exemption cannot be applied — error message will be returned synchronously
  in OrderCreateResponse)".

### 5.3 Soft decline of an MIT charge and the documented recovery path

([Handling Soft Declines](https://developers.payu.com/europe/docs/card-payments/threeds/soft-declines/)):
"In markets where the SCA regulation applies, card issuers possess the
authority to 'soft decline' an authorization that lacks full authentication.
A soft decline refers to a temporary rejection of a payment transaction by a
financial institution due to reasons that can be resolved or mitigated."

Documented recovery ("Resubmitting the Authorization"), verbatim logic:

1. "For each failed card payment, retrieve the transaction data using
   transaction data retrieval."
2. "If the `cardResponseCode` in the response takes the value of `SSD` (soft
   decline), create a new order request with
   `threeDsAuthentication.challengeRequested` set to `MANDATE`. By setting
   `threeDsAuthentication.challengeRequested` to `MANDATE`, you are prompting
   the card issuer to perform full authentication through a challenge."

I.e. the recovery is a **new order** run as an authenticated (customer-present)
transaction — the customer must come back on-session to complete the 3DS
challenge; there is no mechanism to resume the declined order. Merchants
implementing this must "notify our customer support to be excluded from the
solution based on the PayU authentication page" (same page).

The alternative ("Using PayU Authentication Page"): "this approach will result
in PayU returning the redirection link for EVERY non-authenticated (one-click)
card payment that is not flagged as `RECURRING` or `STANDARD_MERCHANT`. To
maintain the existing 'one-click' experience for stored cards, the PayU system
will attempt to utilize one of the available exemptions to avoid Strong
Customer Authentication (SCA) on your behalf. However, if a soft decline
occurs, the handling process will depend on the applicable flow" — for the
browser flow "the payer will be prompted to authenticate on PayU's
authentication page" (same page). Note the flag carve-out: payments flagged
`RECURRING` or `STANDARD_MERCHANT` (the MIT flags) are excluded from the
automatic redirection, so a soft-declined MIT still ends as a failed order
that the merchant must recover per the resubmission logic.

A test soft decline ends as: "an response with a `SUCCESS` status and a
notification with a `CANCELED` status" whose transaction details show `SSD`
(same page) — confirming that an MIT soft decline looks like any other
CANCELED order until the transaction data is fetched.

## 6. Notification traffic

### 6.1 Delivery model

"Notifications, also known as webhooks or IPNs (instant payment
notifications), are messages sent by PayU to inform your server about order or
refund status changes. Notifications are sent only when `notifyUrl` parameter
is provided in the order create request. Refund notifications use the same
address as specified for the order"
([API Reference — Notifications](https://developers.payu.com/europe/api/#tag/Notifications)).
"Notifications are sent in JSON format using POST method"; "Notifications are
sent for orders in the following statuses: `PENDING`,
`WAITING_FOR_CONFIRMATION`, `COMPLETED`, `CANCELED`"
([Payment Lifecycle](https://developers.payu.com/europe/docs/payment-flows/lifecycle/)).
Sender IPs to allowlist are published on the same page (production
185.68.12.10–12, .26–28; sandbox 185.68.14.10–12, .26–28).

### 6.2 Body shape and correlation fields

"Order data provided in notification have the same model as the order returned
in the order retrieve response"
([API Reference — Notifications](https://developers.payu.com/europe/api/#tag/Notifications)).
The documented example body
([Payment Lifecycle](https://developers.payu.com/europe/docs/payment-flows/lifecycle/))
is `{"order": {...}, "localReceiptDateTime": ..., "properties": [...]}` where
`order` carries `orderId`, `extOrderId`, `orderCreateDate`, `notifyUrl`,
`customerIp`, `merchantPosId`, `description`, `currencyCode`, `totalAmount`,
`buyer`, `payMethod`, `products`, `status`; `status` enum on the order model
is `NEW` / `PENDING` / `WAITING_FOR_CONFIRMATION` / `COMPLETED` / `CANCELED`
([API Reference — Order model](https://developers.payu.com/europe/api/#tag/Order)).

- Correlation: `orderId` (PayU-assigned) and `extOrderId` (merchant-assigned;
  "Order identifier assigned by merchant" —
  [API Reference — Create an Order](https://developers.payu.com/europe/api/#tag/Order/operation/create-an-order)),
  plus `properties` with `name: "PAYMENT_ID"` — "the payment identifier,
  displayed on transaction statements as Trans ID and within the transaction
  search option in the management panel"
  ([Payment Lifecycle](https://developers.payu.com/europe/docs/payment-flows/lifecycle/)).
- `localReceiptDateTime` "is only present for the status completed" (same
  page).
- **Neither the `recurring`/`cardOnFile` flag nor the token value appears in
  the notification.** The only payment-method information is
  `payMethod.type` — "`PBL` stands for online or standard transfer,
  `CARD_TOKEN` is a card payment, and `INSTALLMENTS` means a payment via
  PayU|Installments solution" (same page). Deeper detail requires opting in
  via `settings.enrichNotificationWith` (values `authorization`, `buyer`,
  `capture`, `fees`, `payMethod`, `mcp`; "payMethod — payMethod details (by
  default only provided for order in status COMPLETED)"), which "may slightly
  increase the delivery time"
  ([API Reference — Notifications](https://developers.payu.com/europe/api/#tag/Notifications) /
  [Create an Order](https://developers.payu.com/europe/api/#tag/Order/operation/create-an-order)).

MIT charges generate the **same** notification stream as any one-off order —
there is no recurring-specific event type. Zero-amount tokenization orders
notify `PENDING` → `COMPLETED`/`CANCELED`, never `WAITING_FOR_CONFIRMATION`
(§1.4).

### 6.3 Signature verification

([Payment Lifecycle — Verification of Notifications Signature](https://developers.payu.com/europe/docs/payment-flows/lifecycle/)):
"it's crucial to verify the signature value present in the `OpenPayu-Signature`
header for every notification received from PayU servers." Header format:
`sender=checkout;signature=…;algorithm=MD5;content=DOCUMENT` (also duplicated
as `X-OpenPayU-Signature`). Verification, verbatim steps: extract the
`signature` value; "Verify the type of hashing function used to generate the
signature"; "Concatenate the body of the incoming notification (e.g.,
JSONnotification) with the value of the **second key**"; apply the hash;
compare. This is the same mechanism the existing one-off integration uses
(§7).

### 6.4 Redelivery and terminality

"Notifications are sent immediately after a payment status changes. If the
notification is not received by the Shop application, it will be sent again in
accordance with the table below" — 20 attempts: immediately, 1 min, 2 min,
5 min, 10 min, 30 min, 1 h, 2 h, 3 h, 6 h, 9 h, 12 h, 15 h, 18 h, 21 h, 24 h,
36 h, 48 h, 60 h, 72 h
([Payment Lifecycle](https://developers.payu.com/europe/docs/payment-flows/lifecycle/)).
"After PayU sends a notification, it expects a response with a 200 HTTP status
code. If a different status code is received, PayU will attempt to resend the
notification. It's essential for your system to handle such cases where a
notification might be sent multiple times with the same status." Dedup/final
rule, verbatim: "Every notification is sent asynchronously. After your system
receives a notification with the status `COMPLETED`, instruct it to ignore any
further notifications." Delivery may also be throttled ("merchant can receive
only limited number of notification in parallel"), with throttling time
excluded from the `PayU-Processing-Time` header (same page). No ordering
guarantee is documented beyond the COMPLETED-is-final rule.

## 7. Intersection with the existing getpaid-payu integration

The local `python-getpaid-payu` plugin already speaks exactly the API surface
this document describes for one-offs: REST API v2.1 on `secure.payu.com`,
OAuth2 (`client_credentials`) via `oauth_id`/`oauth_secret`, order creation
through `POST /api/v2_1/orders` with per-payment `notify_url`, callback
signature verification (SHA-256 and MD5) using the configured `second_key`,
and status mapping `COMPLETED → payment_captured` / `CANCELED → failed`, plus
capture (`POST …/captures`), cancel (`DELETE /orders/{id}`), and refunds
(plugin README and `docs/concepts.md`, `docs/configuration.md`). What recurring
would add on top, per this document: the `recurring`/`cardOnFile` request
fields and `payMethods.payMethod` with `CARD_TOKEN` (§1.3, §2.1), the
`trusted_merchant` OAuth grant (a second token-acquisition mode keyed by
buyer identity, §1.6), the `GET /paymethods` / `DELETE /tokens/{token}`
endpoints (§3), and transaction-data retrieval for decline reasons (§4.1).
The notification handling path is unchanged (§6).

## 8. Contrast points vs Stripe (facts only)

Structural differences surfaced by this document against the companion Stripe
docs ([merchant-managed](stripe-merchant-managed-recurring.md),
[Billing](stripe-billing-subscriptions.md)):

1. **No first-class setup object.** Stripe saves a card via a SetupIntent
   (its own lifecycle, statuses, events). PayU's save step is an ordinary
   order flagged `FIRST` — either a real purchase or a gated zero-amount
   order (§1.3–1.4); its outcome is an order status, not a setup status.
2. **Token returned synchronously, then re-fetched.** Stripe's PaymentMethod
   id arrives via SetupIntent/webhook and is stored by the merchant; PayU
   returns `TOKC_` in the OrderCreateResponse but documents that merchants
   should *not* store-and-trust it, re-retrieving per payment via
   `GET /paymethods` (§1.5, §3.1) under a buyer-scoped OAuth grant (§1.6).
3. **Customer identity is a merchant-supplied pair, not a provider object.**
   Stripe has a `Customer` object owning payment methods; PayU scopes tokens
   by (POS, buyer email, `ext_customer_id`) with no customer resource to
   create or manage (§1.6).
4. **CIT/MIT is an explicit per-request enum, not an inferred property.**
   Stripe marks MITs via `off_session=true` on a properly-set-up
   PaymentMethod; PayU requires the merchant to label every token charge
   `STANDARD_CARDHOLDER` / `STANDARD_MERCHANT` / `STANDARD` (§2.1), and the
   two parameter families are mutually exclusive per order (§1.2).
5. **Hard dependency on FIRST success.** PayU forbids MIT flags outright if
   the FIRST payment failed (§2.4); Stripe has no equivalent rule (a failed
   initial payment doesn't bar later off-session use of a successfully-set-up
   payment method).
6. **Failure diagnosis is a second API call.** Stripe returns
   `decline_code` synchronously in the 402 error; PayU's MIT decline usually
   surfaces as an asynchronous `CANCELED` notification with no reason, and
   the code (`SSD`, `S51`, …) must be fetched via transaction retrieve
   (§4.1, §5.3).
7. **Soft-decline recovery is a new CIT order.** Stripe re-confirms the *same*
   PaymentIntent on-session after `authentication_required`; PayU documents
   creating a *new* order with `challengeRequested=MANDATE` (§5.3).
8. **No provider retries, no account updater.** Stripe Smart Retries exist
   (Billing-scoped) and Card Account Updater refreshes saved cards; PayU
   documents neither — merchant scheduler only (§2.5, §3.3, §4.3).
9. **Notification model.** Stripe emits typed events (`setup_intent.*`,
   `payment_intent.*`) with merchant metadata on the object; PayU emits one
   untyped order-status notification stream per order to that order's
   `notifyUrl`, correlated by `extOrderId`/`orderId`, carrying neither the
   token nor the recurring flag (§6.2), signed with a static-key hash rather
   than an HMAC-timestamped signature scheme (§6.3), with a documented
   20-step/72 h redelivery schedule (§6.4).
10. **Zero-amount save is gated and card-only.** Stripe's SetupIntent is a
    standard API feature; PayU's zero-amount tokenization requires sales
    enablement per shop/POS and works only for cards, and recurring overall
    requires account-manager configuration (§1.3–1.4).

## Sources

All fetched 2026-07-10 from developers.payu.com (PayU GPO Europe):

- [Recurring Payments](https://developers.payu.com/europe/docs/payment-solutions/cards/recurring/)
- [Card Tokenization (landing)](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/)
- [Creating Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/create-token/)
- [Charging Tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/charge-token/)
- [Retrieving and Deleting tokens](https://developers.payu.com/europe/docs/payment-solutions/cards/tokenization/retrieve-and-delete-token/)
- [Payment Lifecycle (statuses, notifications, signature, redelivery)](https://developers.payu.com/europe/docs/payment-flows/lifecycle/)
- [Transaction Data Retrieval](https://developers.payu.com/europe/docs/payment-flows/transaction-retrieve/)
- [EMV 3DS (landing)](https://developers.payu.com/europe/docs/card-payments/threeds/) / [API Enhancements](https://developers.payu.com/europe/docs/card-payments/threeds/api-enhancements/) / [Handling Soft Declines](https://developers.payu.com/europe/docs/card-payments/threeds/soft-declines/)
- [Card Status Codes](https://developers.payu.com/europe/docs/card-payments/card-status-codes/)
- [Capturing Card Data (Secure Form)](https://developers.payu.com/europe/docs/checkout/secure-form/)
- [PayU GPO Europe REST API reference](https://developers.payu.com/europe/api/) — OpenAPI spec fetched from [`/europe/resources/payu-api-ref.yaml`](https://developers.payu.com/europe/resources/payu-api-ref.yaml) (operations: [Authorize](https://developers.payu.com/europe/api/#tag/Authorize), [Create an Order](https://developers.payu.com/europe/api/#tag/Order/operation/create-an-order), [Retrieve a Transaction](https://developers.payu.com/europe/api/#tag/Order/operation/retrieve-a-transaction), [Payment-Methods](https://developers.payu.com/europe/api/#tag/Payment-Methods), [Token](https://developers.payu.com/europe/api/#tag/Token), [Notifications](https://developers.payu.com/europe/api/#tag/Notifications))
- PayU security requirements PDF linked from the recurring page: [Requirements and recommendations relating to Recurring Payments](https://poland.payu.com/wp-content/uploads/sites/14/2020/07/set_requirements_and_recommendations_relating_to_recurring_payments.pdf) (not fetched; linked for completeness)
- Local plugin cross-reference: `/home/minder/projekty/python-getpaid/python-getpaid-payu/README.md`, `docs/concepts.md`, `docs/configuration.md`
