# Durable state and recovery for money-moving operations

Status: **Accepted; implementation pending for a breaking major release.**

Core will require durable intent reservation and atomic semantic application on
current stored state before promising safe financial mutations. Core remains a
framework-neutral mechanics library: framework wrappers provide persistence and
execution infrastructure. This trades adapter compatibility and per-payment
concurrency for truthful accounting and recoverable uncertain outcomes.

## Context

The reviewed 3.2.0 contract accepts caller-owned mutable payment snapshots and
unconditionally saves them after provider I/O. Independent snapshots can erase
committed amounts and replay history. Callback deduplication cannot prevent a
second outbound financial command, and an in-memory rollback cannot undo a
provider effect. Partial capture, authorization release, refund progress, and
late cumulative observations also need a single financial interpretation.

Existing monetary validation and invalid-provider-result recovery protections
remain prerequisites, not work to undo. This decision changes public contracts;
it is not a claim that the current implementation or any framework adapter
already provides the guarantees below. The existing concepts and API reference
describe the released implementation; implementation slices must update them as
the new behavior lands.

## 1. Core and integration responsibilities

Core owns framework-neutral types, protocols, validation, financial transition
rules, operation orchestration, reconciliation mechanics, and reusable adapter
conformance tests. It does not own a database, ORM models, migrations for an ORM,
a queue, a scheduler, an operator UI, or an authorization system.

Framework wrappers implement durable storage and atomic transitions. For example,
`django-getpaid` owns Django models, ORM transactions, schema/data migrations, and
Django-facing APIs. The application/framework integration drives reconciliation,
scheduling, and operator escalation and enforces operator authorization. No
hidden background worker or recovery scheduler is introduced into core.

### Mandatory storage semantics

A supporting adapter must provide semantic operations to:

- Reserve an operation intent against the current durable payment.
- Apply a normalized observation to current durable state.
- Record an operation outcome and its corresponding financial effects.
- Retrieve operations and discover unresolved/reconciliation-required work after
  a restart, without relying on an exception, log, or caller object.

The payment financial state, affected operation records, and replay evidence
must commit atomically whenever a transition affects them together. Validation
and transition rules remain in core, not independently reimplemented by each
framework. The adapter chooses transactional reload/apply/save, compare-and-set
with explicit conflict handling, or an equivalent implementation. No particular
tables or schema are prescribed; document/JSON storage is acceptable only with
the same atomicity and durability guarantees.

Process-local locks and transactions around an unconditional final save are
insufficient. No database lock/transaction spans provider I/O. A local conflict
may retry the semantic transition against fresh state; it never implicitly
replays a provider command. Unsupported storage adapters fail before submission;
there is no unsafe compatibility fallback.

Mutations address payments by identity and return committed state. A supplied
framework model instance may be an ergonomic wrapper input, but only its identity
is authoritative. Previously loaded objects may remain stale; their financial
fields must not be saved over current records.

## 2. Operation identity and submission

The application supplies an operation ID, stable across retries/restarts and
scoped to a payment. Bind the ID to the operation type and immutable normalized
request parameters. Same ID and parameters refers to the existing operation;
changed parameters conflict. Deliberate separate partial captures/refunds require
new IDs. Equal amounts alone do not establish intent identity.

Reservation durably records the original request semantics, concrete provider
parameters, starting financial totals, and safe correlation. Resolve omitted
amounts against current state during reservation, after applicable validation.
A same-ID retry retrieves that original resolution; it does not rerun default
amount selection against today's balance. Keep correlation for every operation,
not a single overwritten charge/refund identifier in payment metadata.

Reservation and the right to submit must be coordinated across workers. Record
that submission is starting durably before invoking the provider; a duplicate
caller must not independently obtain another submission right. Persisting intent
is not evidence of provider acceptance, and reservation does not prematurely
record settlement.

Only one active mutation per payment is allowed initially, including
provider-pending and unknown operations. Same-ID duplicates retrieve/resume that
operation safely; unrelated distinct commands conflict until resolution.
Authenticated callbacks and reconciliation continue while commands are blocked.
This deliberately gives up simultaneous partial refunds and other per-payment
command concurrency.

### Refund-cancellation exception

A supported cancellation is a separately identified operation targeting a
specific pending refund. Reserve this narrow exception atomically and block
unrelated commands while either result is uncertain. Cancellation stops only the
unexecuted refund portion; it never decreases recorded refunded funds. A racing
settlement remains valid evidence and cannot be overwritten by cancellation.

## 3. Outcomes, provider capabilities, and recovery

Operation states distinguish **reserved**, **submitting**, **provider-pending**,
**succeeded**, **rejected**, and **unknown**. Unknown is nonterminal. Succeeded
means the operation-specific effect is confirmed: provider acceptance alone is
not capture/refund settlement. A confirmed release or refund cancellation does
not mean money was returned. Record **reconciliation required** independently
when evidence cannot be applied consistently or further investigation is needed.

Every processor must accept operation identity and immutable submission
parameters, produce normalized outcomes, and declare capabilities per operation:

- Idempotent submission, including key scope and validity window.
- Outcome lookup and precisely what its results establish.
- Supported capture, authorization release, refund, and refund cancellation.

Idempotent submission and authoritative lookup are optional capabilities, not
assumed properties. The contract covers every mutating operation, including
`prepare`, `charge`, `release_lock`, `start_refund`, and `cancel_refund`.

### Uncertain submission

A timeout, cancellation, expired worker lease, or crash with a submitting record
is not proof that submission failed. First reconcile against provider evidence.
Resubmit only when the provider's documented idempotency guarantee still covers
the same key and immutable payload. Expiry of that guarantee does not become
safe merely because the local record still exists.

If neither safe submission retry nor authoritative lookup is available, require
explicit opt-in to restricted mode: one submission attempt, then an ambiguous
outcome remains unresolved and blocks conflicting commands until reconciliation.
A crash before actual transmission can therefore leave an operation stuck; it
cannot safely be distinguished from execution followed by response loss.

### Reconciliation evidence and actor

Core exposes explicit reconciliation mechanics; the application invokes them.
Authenticated callbacks and authoritative queries tied to the operation are the
preferred evidence. A lookup returning "not found" remains unknown unless the
provider contract conclusively excludes execution in that case. Time alone never
resolves uncertainty.

Provide an explicit auditable operator-resolution path for cases automation
cannot settle. Record actor, reason, and evidence references, enforce financial
invariants, and leave access control to the application. Treat returned provider
data as untrusted; expose/store only safe allowlisted recovery evidence and
correlation in the ordinary result/error contract. Do not indiscriminately log
raw payloads, secrets, or arbitrary result representations.

### Public results and failures

Return a structured operation result with operation ID, explicit outcome,
reconciliation requirement, and a committed payment snapshot when available.
Pending and unknown are ordinary operational outcomes, not disguised rejection.
Validation/conflict and persistence failures remain exceptions. Failure after a
provider response carries payment/operation identity, operation type, and safe
known correlation/evidence; it must be explicitly non-blind-retryable.

Apply the recovery boundary to valid-result FSM failures and repository failures
for all mutating operations, retaining the existing invalid-result protections.
Local failure must not be relabelled provider rejection. Object rollback is not
remote rollback. Terminal operation evidence and its financial effects commit
atomically; a failed final write leaves the earlier durable intent as the recovery
anchor, even if recording a reconciliation flag also fails.

### Cancellation

Propagate cancellation after bounded, cancellation-aware cleanup. Shield only a
short local persistence attempt, never provider I/O. When evidence is available,
attempt to persist it; otherwise retain discoverable submitting/unknown state.
If cleanup fails, times out, or is interrupted again, the pre-submission durable
record remains recoverable. Do not leave orphan background saves. Cancellation
while waiting for the provider is separately an unknown outcome, not evidence of
success, rejection, or cancellation at the provider.

## 4. Financial facts and status

Keep cumulative captured funds, cumulative refunded funds, and remaining
authorization separate from pending-operation information. For a payment:

- `0 <= refunded <= captured <= required`.
- `0 <= remaining authorization <= required - captured`.
- Capture is positive and bounded by remaining authorization and required balance.
- Refund is positive and bounded by `captured - refunded`.
- Release removes the entire positive remaining authorization; it changes neither
  captured nor refunded totals.

Refunding does not reopen capture capacity on the same payment. Collecting
replacement funds requires a new payment. Supported capture/release remains
possible after partial capture; eligibility follows current facts and operation
reservations, not only a legacy status guard.

The public payment status projects these facts with the following precedence:

1. An unresolved refund reports refund-in-progress, retaining financial amounts.
2. Otherwise, positive refunded funds mean fully refunded when refunded equals
   captured, and partially refunded otherwise.
3. Without refunds, captured funds mean paid when captured equals required, and
   partially paid otherwise.
4. Without captured funds, an active hold means authorized; confirmed full
   authorization release means cancelled.

Zero amounts alone do not mean cancellation. Preserve meaningful preparation,
failure, and other nonfinancial lifecycle distinctions where none of the above
settlement rules applies. Remaining authorization and reconciliation requirements
are independently visible; a single payment status is not the whole operation
state. "Partially refunded" must remain distinguishable from "partially paid";
exact public type/member spelling belongs to implementation of this contract.

For required=100, lock(100) -> capture(30) -> capture(70) ends captured=100,
refunded=0, hold=0, paid. Lock(100) -> capture(30) -> release(70) ends captured=30,
refunded=0, hold=0, partially paid. An uncaptured full release remains cancelled.
The historical release-with-captured-funds -> refunded test/docs must change.

## 5. Observations, ordering, and replay ownership

Aggregate financial updates use cumulative totals, not unqualified increments.
Keep operation confirmations correlated with their durable intent. Never add the
requested amount again when a command response follows a callback. For locally
initiated operations, use the reserved starting totals, resolved amount, and
correlated settlement evidence to establish the result, not the caller's current
mutable snapshot. Acceptance alone does not establish a cumulative settlement.

An externally initiated delta-only event needs sufficient provider correlation
and history to establish the cumulative result, or reconciliation. Do not guess a
new total from an isolated delta. This is an explicit integration cost for
providers whose events do not carry cumulative state.

Command responses, callbacks, and polling share the atomic semantic application
boundary. Trustworthy correlation, not equal amounts or "currently active
operation", associates outcomes with intents. A callback may complete an
operation before its submitting worker saves the response; later acceptance
cannot downgrade completion to pending. Contradictory evidence is retained and
flagged for reconciliation. Uncorrelated evidence must not complete an arbitrary
operation.

Acknowledge equal/lower cumulative capture observations without regressing
financial facts or refund progress, with different or missing event IDs as well
as exact duplicates. Process independently valid information alongside stale
financial fields. Delayed cancellation must identify what it cancels; it cannot
erase captured funds or imply a refund. Ambiguous cancellation requires
reconciliation, and impossible new transitions remain errors rather than blanket
ignored exceptions.

If valid evidence establishes genuinely increased capture during or after a
refund, record that financial fact, preserve refunded totals, and require
reconciliation before further commands. Do not automatically refund the difference.
For example, where required >= 120, captured=100/refunded=100 becoming
captured=120/refunded=100 is now partially refunded. Evidence violating hard
financial constraints must remain available for investigation without being
forced into the financial state.

### Trusted replay records

Move replay evidence out of unrestricted `provider_data` into dedicated logical
storage with core-defined semantics and adapter-provided persistence. Commit it
atomically with the affected financial/operation state. Metadata at creation and
on every update must be unable to seed, replace, or erase trusted history.

Scope event identity to its provider/payment context. Detect conflicting reuse
using normalized semantic content, not raw-payload byte equality. Genuine
duplicates are idempotent; a conflicting identity must not suppress a genuine
financial change silently. Invalid metadata is rejected atomically without
losing committed history or financial state. No caller-owned mutable event-list
cache is authoritative, including same-length external list edits.

Retain compact operation and deduplication records for the payment's supported
lifetime, with no automatic expiry in this initial contract. Sensitive provider
evidence has a separate retention policy. Archival/deletion requires an explicit
integration policy that does not turn old retries into fresh transactions. Local
retention never extends a provider's idempotency window.

## 6. Compatibility and rollout

Ship the mandatory adapter/processor contract in a **breaking major release**,
not an optional 3.x safety layer. Narrow compatible fixes may remain in 3.x, but
must not claim these stronger guarantees. Database schema and framework migration
implementations belong in their respective repositories, not this core library.

Provide a framework-neutral serialized-state/migration contract. Preserve legacy
amounts and metadata, but do not promote provider-controlled legacy event IDs to
trusted replay evidence or invent missing historical operation IDs. Readability
is required; historical exactly-once behavior cannot be certified retroactively.

Only unambiguously mapped payments may mutate after migration. Ambiguous
financial records and pre-existing pending operations require reconciliation
first. Require a coordinated writer cutover: old unconditional-save workers stop
before new-contract writes begin. Do not mix old and new writers against the same
payment state. Integration migration/maintenance guidance must describe recovery
and cutover before the corresponding implementation is declared usable.

## Rejected alternatives and consequences

- **Optional durability / unconditional-save fallback:** retains compatibility but
  cannot deliver the promised multi-worker or crash-recovery guarantees.
- **Core-owned ORM/storage/scheduler:** simplifies one deployment shape but crosses
  the library boundary and constrains framework wrappers unnecessarily.
- **Prescribed SQL locks or versions:** an implementation option, not the portable
  public contract; semantic atomicity is what consumers need.
- **Process-local locks or locks across provider calls:** respectively fail across
  workers or hold scarce transactional resources during uncertain remote I/O.
- **Amount-derived IDs or automatic fresh IDs on retry:** confuse separate partial
  intents with retries and fail to survive process restart correctly.
- **Blind retry, timeout-as-rejection, or lease-expiry resubmission:** can duplicate
  a remote effect. Restricted providers may instead need manual intervention.
- **Concurrent unrelated commands per payment:** deferred to avoid overlapping
  financial reservations and a substantially larger correctness surface.
- **One status as the full financial model, or release-as-refund:** cannot represent
  remaining authorization, captured funds, and refund progress truthfully.
- **Replay history in provider metadata or blanket ignoring transition errors:**
  respectively permits history forgery or conceals genuinely invalid evidence.
- **Unbounded shielding / background saves:** hides cancellation and creates
  unowned work without guaranteeing durability.

Costs include a breaking integration upgrade, durable record growth, reduced
per-payment concurrency, possible manual recovery, and provider normalization
work. These are explicit trade-offs, not claims of distributed exactly-once
execution. In-memory core tests do not certify a real adapter or provider.

## Required implementation evidence

Conformance tests must use independent snapshots and workers, not only shared
mutable fake objects. Cover full/partial capture races, capture/refund races,
duplicate/distinct/conflicting event IDs, metadata erasure/prepopulation/malformed
types and cache edits, both partial-capture examples, callback/PULL reordering
through pending/partial/full refunds, and genuinely new versus impossible money.

Recording/idempotent provider fakes must cover sequential and concurrent retries,
distinct partial intents, changed-parameter conflicts, frozen default amounts,
response loss, expired/absent provider idempotency, callback-before-response,
refund-cancellation races, and persistence failure after acceptance. No second
transaction may arise from the same intent under the declared capabilities;
restricted mode must not resubmit uncertain work.

For every mutating operation, test provider rejection separately from successful
results followed by FSM or persistence failure. Use deterministic event barriers
for cancellation at final save and while awaiting the provider; prove cancellation
propagation, bounded cleanup, no orphan tasks, and discoverable recovery state
including when cleanup persistence fails. Framework wrappers separately prove
real transactional behavior and migration/cutover compatibility. No live-provider
or end-to-end assurance follows from the core conformance suite alone.
