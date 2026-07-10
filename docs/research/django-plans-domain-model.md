# django-plans: domain model review for core's plan/pricing primitives

Scope note: this doc reviews the django-plans domain model as input to the
design of plan/pricing primitives in getpaid-core. django-plans is the
reference "plans layer" that may later be re-based onto core's primitives, so
the deliverable answers two questions: (a) which vocabulary core's primitives
must support so a re-basing is possible (§8), and (b) which django-plans
concepts confirm the boundary of what stays above core (§9). Sections 1–7 are
facts only, each claim cited to a file path and class/field name in the local
checkout of <https://github.com/django-getpaid/django-plans> at commit
`45e9a06fdee4c7662f2df44aa3ba134a4bd3c1c1` (2026-06-03), reviewed 2026-07-10.
Provider shapes referenced in the analytical sections come from the companion
docs [`stripe-billing-subscriptions.md`](stripe-billing-subscriptions.md)
(provider-managed) and
[`stripe-merchant-managed-recurring.md`](stripe-merchant-managed-recurring.md)
(card on file). The SwampCastle `django_plans` room was queried for prior
decisions; every hit was an indexed source-file snippet from this same
checkout, so no memory drawers are cited — the code is authoritative
throughout.

Unless stated otherwise, all model facts are from `plans/base/models.py`
(abstract models; concrete swappable subclasses live in `plans/models.py`).

## 1. Plan — the catalog entity

`AbstractPlan` (`plans/base/models.py`) fields:

- `name`, `slug` (unique), `description`, `url` (optional "more info" link
  "for clickable pricing table headers").
- `default` — nullable boolean, `unique=True`, help text: "Both 'Unknown' and
  'No' means that the plan is not default". At most one plan in the system is
  the default; `AbstractPlan.get_default_plan()` returns it or `None`.
- `available` — "Is still available for purchase".
- `visible` — "Is visible in current offer".
- `customized` — nullable FK to `AUTH_USER_MODEL`: a per-user plan. The class
  docstring: "A plan can [be] customized (referred to user) which means that
  only this user can purchase this plan and have it selected."
- `quotas` — `ManyToManyField("Quota", through="PlanQuota")` (§6.1).
- Ordering: `AbstractPlan` extends `OrderedModel` (django-ordered-model) with
  `Meta.ordering = ("order",)` — catalog display order is a first-class model
  concern, separate from any billing field.

Visibility vs availability semantics (class docstring, verbatim): "Plan is
displayed on the list of currently available plans for user if it is visible.
User cannot change plan to a plan that is not visible. Available means that
user can buy a plan. If plan is not visible but still available it means that
user which is using this plan already will be able to extend this plan again.
If plan is not visible and not available, he will be forced then to change
plan next time he extends an account." So `visible=False, available=True` is
the "grandfathered plan" state.

Default-plan mechanics:

- `AbstractUserPlan.create_for_user` (`plans/base/models.py`) creates the
  user's `UserPlan` on the default plan with `active=False, expire=None`;
  it is wired to `post_save` on the user model in `plans/listeners.py` —
  `set_default_user_plan`.
- `AbstractPlan.get_current_plan(user)` falls back to the default plan when
  the userplan is expired (or the user is anonymous / has no userplan), and
  raises `ValidationError("User plan has expired")` if the default plan is
  missing or not free — i.e. the default plan is expected to be the free tier
  users land on after expiry.
- Free plan test: `AbstractPlan.is_free()` returns
  `self.planpricing_set.count() == 0` — a free plan is a plan with **no
  PlanPricing rows**, not a zero price.

## 2. Pricing and PlanPricing — period and price as separate axes

`AbstractPricing` (`plans/base/models.py`) is a named period:

- `name` (e.g. "month", "year") and `period` — `PositiveIntegerField`,
  **days**, `default=30`, nullable. Class docstring: "Type of plan period that
  could be purchased (e.g. 10 days, month, year, etc)". `__str__` renders
  `"<name> (<period> days)"`. There is no interval unit — a month is
  `period=30`, a year `period=365`; billing arithmetic everywhere is
  `timedelta(days=pricing.period)`.

`AbstractPlanPricing` is the Plan×Pricing join carrying the money:

- `plan` FK, `pricing` FK, `price` — `DecimalField(max_digits=7,
  decimal_places=2)`. **No currency field** (see below).
- `has_automatic_renewal` — `BooleanField(default=False)`, help text "Use
  automatic renewal if possible?". Recurrence eligibility is a property of the
  **(plan, period) offer**, not of the plan or the period alone.
- `visible` — per-offer display flag ("Is visible in current offer").
- `order` — integer sort key; `Meta.ordering = ("order", "pricing__period")`.

Currency is **global configuration, not per-price**: `get_currency()` in
`plans/utils.py` reads `settings.PLANS_CURRENCY` and raises
`ImproperlyConfigured` unless it is a 3-letter code; `AbstractOrder.recalculate`
stamps `self.currency = get_currency()` on every order. (`plans/conf.py`
holds only `PLANS_TAX`, `PLANS_TAXATION_POLICY`, `PLANS_APP_VERBOSE_NAME`;
the currency setting lives in `utils.py`.) `Order.currency` and
`RecurringUserPlan.currency` are 3-char snapshot fields (`Order.currency`
has `default="EUR"`), so records carry a currency, but the system assumes one
deployment-wide currency at order-creation time.

Free plans: modeled by *absence* of PlanPricing rows (§1). Consequences wired
through the lifecycle: `UserPlan.extend_account` sets `expire = None` when the
target plan has no pricings ("Assume no expiry date for plans without
pricing"); `UserPlan.initialize` only sets an expiry "Plans without pricings
don't need to expire"; `PlanChangePolicy._calculate_day_cost` returns cost 0
for free plans.

## 3. UserPlan — the current-subscription entity

`AbstractUserPlan` (`plans/base/models.py`), docstring "Currently selected
plan for user account":

- `user` — `OneToOneField(AUTH_USER_MODEL)`: exactly one subscription per
  user, ever. There is no history entity; history lives in Orders (§6.3).
- `plan` FK, `expire` — **`DateField`**, nullable, `default=None`,
- `active` — `BooleanField(default=True)`.

Expiry model:

- `is_expired()` — `False` if `expire is None` ("nullable = never expires"),
  else `expire < now().date()`. Day resolution; a plan expiring today is
  still valid today.
- `days_left()` — `(expire - today).days`, `None` when `expire is None`.
- `active` is decoupled from `expire`: activation is gated on quota
  validation, not dates. `clean_activation()` runs
  `plan_validation(self.user)` (`plans/validators.py`) and calls `activate()`
  only when no `required_to_activate` errors exist, else `deactivate()`.
  `activate()`/`deactivate()` flip the flag and send `account_activated` /
  `account_deactivated` (`plans/signals.py`).
- `initialize()` — first-use setup: for plans with pricings, sets
  `expire = now + PLANS_DEFAULT_GRACE_PERIOD` (default 30 days) and
  activates. Triggered via the `activate_user_plan` signal / a
  django-registration hook (`plans/listeners.py` —
  `initialize_plan_generic`).

`extend_account(plan, pricing)` — the single mutation entry point, with two
distinct semantics on the `pricing` argument (docstring: "if pricing is None
then account will be only upgraded"):

- **`pricing=None` → plan change**: sets `self.plan = plan`, leaves `expire`
  untouched (except `expire=None` for free target plans), sends
  `account_change_plan`, optionally emails
  (`PLANS_SEND_EMAILS_PLAN_CHANGED`). No time is bought.
- **`pricing` set → same-plan extension**: new expiry from
  `get_plan_extended_until(plan, pricing)` =
  `get_plan_extended_from(plan) + timedelta(days=pricing.period)`, where
  `get_plan_extended_from` returns the current `expire` if the plan is the
  same and unexpired, otherwise `now().date()` — i.e. **stacking** for
  renewals, **fresh start** after a gap. If the order's plan differs from the
  current plan, the code treats it as an anomaly ("This should not ever
  happen…"): it refuses (`status=False`) when the current paid plan is
  unexpired, and switches plan + extends when the current plan is free or
  expired.
- On success, `clean_activation()` re-runs quota validation (§6.1) — buying
  time does not guarantee activation.

`reduce_account(pricing, order=None)` is the refund inverse (rewinds `expire`
using per-order snapshots — `Order.userplan_expire_before` etc., §6.3).

Expiration machinery (`plans/tasks.py`, exposed as management commands in
`plans/management/commands/expire_accounts.py` and
`autorenew_accounts.py` — cron/celery-beat driven, no built-in scheduler):

- `expire_account()` task: selects active userplans with
  `expire < today` and calls `UserPlan.expire_account()`, which deactivates,
  sends a templated email, and emits the `account_expired` signal.
- Notification lead times: `PLANS_EXPIRATION_REMIND` (list of
  days-before-expiry ints); the same task calls
  `UserPlan.remind_expire_soon()` (templated email with `days_left`) for
  accounts whose `expire` matches one of the lead days.

## 4. RecurringUserPlan — the card-on-file extension

`AbstractRecurringUserPlan` (`plans/base/models.py`), docstring: "OneToOne
model associated with UserPlan that stores information about the plan
recurrence."

Fields:

- `user_plan` — `OneToOneField("UserPlan", related_name="recurring")`: 1:1,
  at most one recurring agreement per subscription.
- `token` — `CharField(max_length=255)`, nullable; help text: "Token, that
  will be used for payment renewal. **Depends on used payment provider**" —
  an opaque provider credential (Stripe `pm_…`/customer id, PayU
  `payMethods` token, …).
- `payment_provider` — `CharField(max_length=255)`, nullable; "Provider, that
  will be used for payment renewal" — a free-form provider name, used only as
  a filter (`autorenew_account(providers=…)` filters
  `userplan__recurring__payment_provider__in`, `plans/tasks.py`).
- `pricing` FK, `amount` (`Decimal(7,2)`), `tax` (nullable — "Tax=None is
  when tax is not applicable"), `currency` (3 chars) — a **frozen snapshot of
  the renewal offer**, copied from the completing order by
  `UserPlan.set_plan_renewal` (`self.recurring.pricing = order.pricing;
  …amount = order.amount; …tax = order.tax; …currency = order.currency`).
- `renewal_triggered_by` — `IntegerField` over the
  `RENEWAL_TRIGGERED_BY` enumeration `OTHER(1)` / `USER(2)` / `TASK(3)`;
  help text: "USER = user-initiated renewal, TASK =
  autorenew_account-task-initiated renewal, OTHER = renewal is triggered
  using another mechanism". This replaced a boolean
  (`_has_automatic_renewal_backup_deprecated`, kept with
  `db_column="has_automatic_renewal"` plus deprecated property shims). The
  three-way split distinguishes merchant-clock renewal (TASK), one-click
  stored-token renewal by the user (USER), and provider-managed renewal
  (OTHER — e.g. Stripe Billing owning the schedule;
  `docs/source/plans_recurrence.rst`: "the RecurringUserPlan can indicate
  that another mechanism is used to automatically renew the plans").
- `token_verified` — `BooleanField(default=False)`; help text: "The recurring
  token has been verified **by at least one payment** to be working." This is
  the FIRST-payment gate: `UserPlan.has_automatic_renewal()` requires
  `recurring.renewal_triggered_by != USER` **and** `recurring.token_verified`,
  and the `autorenew_account` task filters `token_verified=True` — an
  unverified token is never charged automatically.
- Card metadata: `card_expire_year`, `card_expire_month`,
  `card_masked_number` — display/expiry-warning data only; nothing in the app
  consumes them.
- `last_renewal_attempt` — `DateTimeField`, stamped by the task before each
  attempt; used for retry dedup (below).

Renewal flow the app expects (merchant-clock shape):

1. `autorenew_account()` (`plans/tasks.py`) selects userplans with
   `renewal_triggered_by=TASK, token_verified=True` whose `expire` falls
   inside the schedule window. Scheduling config: `PLANS_AUTORENEW_SCHEDULE`
   — a list of `timedelta` lead times before expiry; for each schedule entry
   the task picks accounts with `expire <= now + schedule` that haven't been
   attempted inside that window (`last_renewal_attempt < expire - schedule`
   or never), bounded by `PLANS_AUTORENEW_MAX_DAYS_AFTER_EXPIRY` (default 30
   days) — so a list of lead times doubles as a **retry ladder**, and renewal
   attempts continue up to 30 days past expiry. (Legacy:
   `PLANS_AUTORENEW_BEFORE_DAYS`/`_HOURS`, deprecated.)
2. For each account the task stamps `last_renewal_attempt` and emits the
   **`account_automatic_renewal` signal** (`plans/signals.py`: "Try to renew
   the account automatically. Should renew the user's UserPlan by recurring
   payments."). django-plans does **not** charge anything itself.
3. The merchant's receiver implements the charge
   (`docs/source/plans_recurrence.rst`): call
   `user.userplan.recurring.create_renew_order()` — which creates an `Order`
   from the frozen `recurring.pricing/amount/tax/currency` (not from the
   current catalog price) and re-runs tax calculation via
   `order.recalculate(...)` — then charge the provider with
   `recurring.token`, and on success call `order.complete_order()`.
4. Operational wrappers on the task: `providers` filter, `throttle_seconds`,
   `catch_exceptions` (log + `mail_admins`), `dry_run`
   (`plans/management/commands/autorenew_accounts.py`).

Provider coupling: there is none beyond the opaque `token`/`payment_provider`
strings and the signal. The only payment-library-specific code in the app is
an optional import-guarded hook to legacy django-getpaid in
`plans/listeners.py` (`from getpaid.signals import user_data_query` inside
`try/except ImportError`, used to supply the buyer email). The provider
interface is therefore fully abstract: *store token → task emits signal →
merchant charges → merchant completes order*.

`set_plan_renewal` wipes all fields first (`set_all_fields_default()` — "We
don't want to mix the old and new values"), so switching provider/pricing
resets `token_verified=False` and the whole agreement.

## 5. Plan-change policy machinery

`plans/plan_change.py`:

- `PlanChangePolicy._calculate_day_cost(plan, period)` — picks the "most
  fitted" PlanPricing for the remaining period (iterates
  `planpricing_set.order_by("-pricing__period")` and stops at the first
  pricing with `pricing.period <= period`, else keeps the smallest), then
  computes a **day cost** = `price / pricing.period`, quantized to
  `Decimal("1.00")`. Free plans cost 0.
- `PlanChangePolicy.get_change_price(plan_old, plan_new, period)` — returns
  `None` ("no payment is required") when `period < 1` or when the new plan's
  day cost is ≤ the old plan's; otherwise
  `period * (new_day_cost - old_day_cost)`. The proration basis is
  **remaining days × day-cost difference** — the user keeps their expiry date
  and pays only the value delta of the remaining time.
- `StandardPlanChangePolicy` refines `_calculate_final_price`: downgrades
  return `DOWNGRADE_CHARGE` (default `None` = free), upgrades cost
  `period * day_cost_diff * (1 + UPGRADE_PERCENT_RATE/100) + UPGRADE_CHARGE`
  (defaults 10% / 0), with amounts below `FREE_UPGRADE` collapsing to `None`.
  Docstring example: 23 days left, €20/30d → €50/30d gives
  `23 * 1.00 € * 110% = 25.30 €`.
- The policy is pluggable via `settings.PLANS_CHANGE_POLICY` (dotted path,
  loaded through `plans/importer.py` — `import_name`); `get_change_price`
  falls back to `period = 30` when `expire is None`.

Switch semantics (`plans/views.py` — `ChangePlanView` docstring: "A view for
instant changing user plan when it does not require additional payment"):
when the computed price is `None`, the switch happens **immediately** via
`extend_account(plan, pricing=None)`; when a price is due, an upgrade `Order`
with `pricing=None` is created (`CreateOrderPlanChangeView`) and the switch
happens at `complete_order()`. In both cases the change is
switch-now-keep-expiry — there is no "downgrade at period end" concept
anywhere in the model. Free-plan transitions: changing to a free plan clears
`expire` (§3); changing *from* a free plan is always an upgrade with day cost
0 on the old side.

## 6. The above-core layer: quotas, invoicing, orders

### 6.1 Quotas and validators

- `AbstractQuota` (`plans/base/models.py`) — "Single countable or boolean
  property of system (limitation)": `codename` (unique), `name`, `unit`,
  `description`, `is_boolean`, plus `OrderedModel` ordering and a pricing-table
  `url`.
- `AbstractPlanQuota` — Plan×Quota join with `value`
  (`BigIntegerField(default=1, null=True)`; `AbstractPlan.get_quota_dict()`
  flattens to `{codename: value}`).
- Validators (`plans/validators.py`): `QuotaValidator` base
  (`required_to_activate = True`, `on_activation` hook), `ModelCountValidator`
  ("no more than quota number of objects [of a] given model" — treats
  `quota is None` as unlimited), `ModelAttributeValidator`.
  `plan_validation(user, plan=None, on_activation=False)` loads a
  `{quota_codename: validator_dotted_path}` mapping from
  `settings.PLANS_VALIDATORS` and buckets errors into
  `required_to_activate` vs `other`; `UserPlan.clean_activation` gates
  activation on the first bucket (§3). `plans/quota.py` — `get_user_quota`
  resolves the effective quota dict via `Plan.get_current_plan` (i.e. the
  default plan's quotas apply after expiry).

### 6.2 Invoicing, VAT, billing info

- `AbstractBillingInfo` (`plans/base/models.py`) — 1:1 with user: `tax_number`
  (VAT ID), name/street/zipcode/city/`country` (django-countries), optional
  shipping fields; `clean_tax_number` validates EU VAT IDs via
  `stdnum.eu.vat` and cross-checks the country prefix.
- `AbstractInvoice` — a full accounting document: four `INVOICE_TYPES`
  (`INVOICE`, `DUPLICATE`, `PROFORMA`, `CREDIT_NOTE`) with dedicated managers;
  gap-free numbering via django-sequences with counter reset policy
  `PLANS_INVOICE_COUNTER_RESET` (daily/monthly/annually/callable) and
  templated `full_number` (`PLANS_INVOICE_NUMBER_FORMAT`); issuer data from
  `settings.PLANS_INVOICE_ISSUER`; buyer/shipping data copied from
  BillingInfo; amounts copied from the order (`copy_from_order`);
  cancellation via credit notes (`cancel_invoice`,
  `create_partial_credit_note`).
- Lifecycle wiring (`plans/listeners.py`): proforma on `Order` creation,
  invoice on the `order_completed` signal, email on invoice creation.
- Taxation (`plans/taxation/`): `EUTaxationPolicy` (`eu.py`, VIES lookups),
  `ru.py`; selected by `settings.PLANS_TAXATION_POLICY`;
  `Order.recalculate` resolves the tax rate per buyer country/VAT number
  with a session cache (`plans/utils.py` — `get_tax_rate`,
  `TaxCacheService`).

### 6.3 Order — where a payment library plugs in

`AbstractOrder` (`plans/base/models.py`) — "supports only one item per order",
defined by `plan` + `pricing` ("If only plan is provided (with pricing set to
None) this means that user purchased a plan upgrade"):

- `STATUS` enumeration: `NEW` / `COMPLETED` / `NOT_VALID` / `CANCELED` /
  `RETURNED`. `is_ready_for_payment()` — `NEW` and younger than
  `PLANS_ORDER_EXPIRATION` (default 14 days).
- `amount`, `tax`, `currency`, `total()` (net × (1+tax)), `completed`
  timestamp.
- `complete_order()` — the **payment-success entry point**: row-locks the
  order and the UserPlan (`select_for_update`), snapshots pre-extension
  UserPlan state (`userplan_expire_before`, `userplan_active_before`,
  `userplan_plan_before`, `plan_extended_from`/`plan_extended_until`), calls
  `UserPlan.extend_account`, sets `COMPLETED`/`NOT_VALID`, emits
  `order_completed`. Idempotent: returns `False` if already completed.
- `return_order()` — the **refund entry point**: validates state, calls
  `reduce_account(pricing, order=self)` which uses the snapshots to rewind
  `expire` by the exact days the order added, and cancels invoices via credit
  notes.

The contract with any payment library is exactly: *create Order → collect
`order.total()` in `order.currency` → call `order.complete_order()` on
success / `return_order()` on refund*. Nothing in the Order knows payment
internals; conversely the plans layer expects the payment side to survive on
these two calls plus (for recurring) `recurring.token`.

## 7. Signals, events, and extension points

Signals (`plans/signals.py`, docstrings verbatim where quoted):

| Signal | Meaning |
|---|---|
| `order_started` | "Sent after order was started (awaiting payment)" |
| `order_completed` | "Sent after order was completed (payment accepted, account extended)" |
| `account_automatic_renewal` | "Try to renew the account automatically. Should renew the user's UserPlan by recurring payments. If this succeeds, the plan should be extended." — the payment library's inbound hook (§4) |
| `account_activated` / `account_deactivated` | activation flag transitions (§3); deactivation "could be not expired, but does not meet quota limits" |
| `account_expired` | "account has expired due to plan expire date limit", sent alongside deactivation |
| `account_change_plan` | "plan was changed after order completion" |
| `activate_user_plan` | inbound: app should call it after user registration confirmation |
| `user_language` | inbound query for the user's email language |

Extension points:

- **Swappable models**: every entity is an `Abstract*` class in
  `plans/base/models.py`; concrete defaults in `plans/models.py` declare
  `swappable = swappable_setting("plans", "<Name>")` (django-swapper), and
  `BaseMixin.get_concrete_model()` resolves via `swapper.load_model` — the
  whole schema is replaceable per deployment.
- **Dotted-path policy/config loading**: `plans/importer.py` — `import_name`
  powers `PLANS_CHANGE_POLICY`, `PLANS_VALIDATORS`, `PLANS_TAXATION_POLICY`.
- **Enumeration** (`plans/enumeration.py`): int-backed named choices used for
  `Order.STATUS`, `Invoice.INVOICE_TYPES`,
  `RecurringUserPlan.RENEWAL_TRIGGERED_BY`.
- **Payment-library touchpoints today**: the `account_automatic_renewal`
  receiver contract (§4), `Order.complete_order()`/`return_order()` (§6.3),
  and the optional legacy `getpaid.signals.user_data_query` hook in
  `plans/listeners.py`. That is the entire coupling surface.

---

## 8. Vocabulary core's primitives must support for a future re-basing

Analytical section. Each item: the primitive vocabulary, the django-plans
fact it comes from, and where it tensions with the Stripe facts in the
companion docs (and the PayU FIRST gate).

1. **Plan / Period / Price as three separate axes.** django-plans models the
   catalog as Plan × Pricing (named period) × PlanPricing (`price`), so one
   plan sells at many periods and one period is shared across plans
   (`AbstractPlan`, `AbstractPricing`, `AbstractPlanPricing`). *Tension*:
   Stripe collapses period+amount into a single Price object hanging off a
   Product (`stripe-billing-subscriptions.md` §1.1). Core's primitive should
   be the **(plan-ref, period, amount) triple** — flat enough to map to a
   Stripe Price, decomposed enough that django-plans can rebuild its two-axis
   catalog on top.

2. **Period expressed as an integer day count.** `Pricing.period` is
   `PositiveIntegerField` days (default 30) and every lifecycle computation is
   `timedelta(days=period)` (`extend_account`, `get_plan_extended_until`,
   proration). *Tension*: Stripe periods are `interval` + `interval_count`
   (calendar months/years, anchor-shifted via `billing_cycle_anchor`), and on
   basil the current period lives per subscription-item. A core period type
   that only knows days cannot round-trip "1 calendar month"; one that only
   knows interval units cannot express django-plans' "10 days". Core needs a
   period vocabulary admitting both (day-count *and* calendar-interval), with
   day-count as the arithmetic-friendly form django-plans requires.

3. **Recurrence eligibility as a per-offer flag; trigger mode as a per-agreement
   enum.** `PlanPricing.has_automatic_renewal` marks which (plan, period)
   offers *may* auto-renew; `RecurringUserPlan.renewal_triggered_by`
   (USER / TASK / OTHER) records who actually drives a given agreement's
   renewals. The USER/TASK/OTHER split maps 1:1 onto core's planned shapes:
   USER = one-click reuse of a stored token (CIT with saved credential),
   TASK = merchant-managed off-session MIT
   (`stripe-merchant-managed-recurring.md`), OTHER = provider-managed
   (Stripe Billing, `stripe-billing-subscriptions.md`). Core's subscription
   primitive must carry this **trigger-mode axis** explicitly or a re-based
   django-plans cannot represent all three of its documented modes
   (`docs/source/plans_recurrence.rst`).

4. **Token + verified-flag agreement shape.** The stored-credential agreement
   is: opaque `token` (provider-defined), `payment_provider` name,
   `token_verified` boolean, card display metadata
   (`card_expire_year/month`, `card_masked_number`), and
   `last_renewal_attempt` (`AbstractRecurringUserPlan`). `token_verified`
   gates all automatic charging (`UserPlan.has_automatic_renewal`,
   `autorenew_account` filter). This is exactly the PayU FIRST-success gate
   (a recurring token becomes chargeable only after a successful FIRST
   payment). *Tension*: django-plans defines verification as "verified **by
   at least one payment**" (field help text), but Stripe verifies a
   credential with **no payment at all** — a `succeeded` SetupIntent with
   `usage=off_session` (`stripe-merchant-managed-recurring.md` §1). Core's
   flag should mean "credential confirmed usable for off-session charging",
   with *how* it was confirmed (first payment vs setup flow) as provider
   detail — a superset of both.

5. **Frozen renewal terms on the agreement, not live catalog reads.**
   `set_plan_renewal` snapshots `pricing/amount/tax/currency` from the
   completing order onto the agreement, and `create_renew_order` builds
   renewal orders from **those snapshots** (only tax is recomputed). Catalog
   price changes never silently reprice an existing subscriber. *Consonant*
   with Stripe, where the subscription pins Price objects; core's agreement
   primitive needs its own amount/currency/period fields rather than a
   foreign key that reads live prices.

6. **Merchant-clock renewal as an emitted event, charge delegated outward.**
   django-plans owns the schedule (`PLANS_AUTORENEW_SCHEDULE` — a list of
   lead-time deltas doubling as a retry ladder, `last_renewal_attempt` dedup,
   `PLANS_AUTORENEW_MAX_DAYS_AFTER_EXPIRY` retry horizon) and emits
   `account_automatic_renewal`; the receiver charges and calls
   `complete_order()` (`plans/tasks.py`, `plans_recurrence.rst`). This is
   precisely core's clockless division of labor for the merchant-managed
   shape: the plans layer is the scheduler-caller, core supplies
   *charge-with-stored-credential* + idempotent completion. It also matches
   the Stripe fact that nothing retries bare PaymentIntents
   (`stripe-merchant-managed-recurring.md` §4.4) — the retry ladder must
   live in the caller, and django-plans proves the caller wants to own it.
   *Tension*: for the provider-managed shape (Stripe Billing) the schedule
   inverts — payments arrive unprompted and django-plans' equivalent is
   `renewal_triggered_by=OTHER` with the receiver machinery bypassed; core
   must let both directions coexist.

7. **Day-based proration arithmetic as pluggable policy, given day-count
   primitives.** `PlanChangePolicy` prorates as *remaining days × (new day
   cost − old day cost)*, day cost = `price / pricing.period` quantized to
   cents; the policy class itself is swappable (`PLANS_CHANGE_POLICY`).
   *Tension*: Stripe "prorates to the second" and computes credits
   differently per `billing_mode` (`stripe-billing-subscriptions.md` §6.2).
   Core should therefore **not** hard-code any proration formula; what it
   must supply is the inputs django-plans' policy needs — remaining time in
   days (`days_left()`), per-offer price and period — and an "ad-hoc amount"
   charge (django-plans models it as an Order with `pricing=None`).

8. **Expiry-date-driven, switch-immediately lifecycle.** The subscription
   state is `(plan, expire: date|None, active: bool)`; renewal = date
   extension with stack-vs-fresh-start logic (`get_plan_extended_from`);
   `expire=None` means never-expires (free plans); plan changes take effect
   immediately and keep the expiry (§5); refunds rewind the date by exact
   snapshot deltas (`reduce_account`). *Tension*: Stripe's lifecycle is a
   status graph (`incomplete`…`canceled`) anchored on billing-cycle
   boundaries, with no merchant-visible "paid-through date" as the primary
   key of state, and with `cancel_at_period_end`/schedules for deferred
   changes. Core's subscription primitive needs a **paid-through timestamp**
   as first-class data (django-plans' native language) *alongside* a status
   enum (the provider-managed language); one cannot substitute for the
   other. Note django-plans has no dunning states at all — `past_due` is
   implicit in "expired but still inside `PLANS_AUTORENEW_MAX_DAYS_AFTER_EXPIRY`".

9. **Single-currency deployment assumption, currency still stamped per
   record.** Prices (`PlanPricing.price`) carry no currency; orders get
   `settings.PLANS_CURRENCY` at creation (`utils.get_currency`,
   `Order.recalculate`) and agreements snapshot it. *Tension*: Stripe Prices
   are inherently per-currency. Core primitives should keep
   (amount, currency) pairs on every money-bearing record (django-plans
   already stores the 3-char code everywhere), while tolerating callers whose
   catalog is currency-less because currency is deployment config.

10. **Per-user plan variants and offer-visibility flags are catalog, not
    billing.** `Plan.customized` (per-user plan), `default` (unique fallback
    plan), `visible`/`available` (offer lifecycle), `OrderedModel` ordering,
    and `PlanPricing.visible` are pure catalog/display vocabulary with no
    provider counterpart. Core does not need to model them — but core's
    plan/price references must be plain enough (IDs + amounts) that a layer
    carrying these flags can sit on top without fighting core's schema.

## 9. Above-core boundary confirmation

Analytical section. django-plans concepts that confirm what stays **above**
core (one line each, with the citing model/file):

- **Quotas / entitlements / validators** — `AbstractQuota`,
  `AbstractPlanQuota` (`plans/base/models.py`), `plans/validators.py`
  (`plan_validation`, `ModelCountValidator`): entitlement is app-domain
  (counts of *the application's* models); core sees none of it.
- **Activation semantics decoupled from payment** — `UserPlan.active` +
  `clean_activation` gate activation on quota validation, not on payment
  state (`plans/base/models.py`): "is the account operational" is an
  application decision layered on top of "is it paid".
- **Invoicing, numbering, credit notes** — `AbstractInvoice` with
  django-sequences numbering, `PLANS_INVOICE_NUMBER_FORMAT`,
  `cancel_invoice`/partial credit notes (`plans/base/models.py`):
  jurisdiction-grade accounting documents, entirely above payment execution.
- **Taxation / VAT** — `plans/taxation/eu.py` (`EUTaxationPolicy`, VIES),
  `BillingInfo.clean_tax_number` (stdnum EU VAT validation),
  `Order.recalculate` tax resolution: tax is buyer-jurisdiction logic the
  payment core never touches.
- **Billing/shipping identity** — `AbstractBillingInfo`
  (`plans/base/models.py`): invoice-party data, not payment-credential data.
- **Order ledger with reversible completion** — `AbstractOrder` snapshots
  (`userplan_expire_before` et al.) and `return_order` symmetry
  (`plans/base/models.py`): mapping money events onto subscription-time
  bookkeeping is the plans layer's job; core only reports payment/refund
  outcomes.
- **Notification scheduling and email** — `PLANS_EXPIRATION_REMIND` +
  `remind_expire_soon`, `send_template_email` throughout
  (`plans/tasks.py`, `plans/base/models.py`, `plans/contrib.py`): user
  communication cadence is application policy.
- **Plan-change pricing policy** — `plans/plan_change.py`
  (`StandardPlanChangePolicy`, `PLANS_CHANGE_POLICY` swap point): proration
  is explicitly pluggable *in django-plans itself*, confirming it must not be
  baked into core (§8.7).
- **Catalog display concerns** — `Plan.visible/available/default/customized`,
  `OrderedModel` ordering, pricing-table `url` fields
  (`plans/base/models.py`): pure presentation/offer management (§8.10).
