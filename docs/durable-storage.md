# Durable Storage Contract

This page describes what a framework adapter must provide for core to
promise safe money movement, and where the boundary to the next major
release runs. The decision behind it is
[ADR 0001](adr/0001-durable-money-operations.md); this page documents the
mechanics that have landed in `getpaid_core.durable`.

The released 3.x flow applies updates to a payment object the caller
handed in and saves it unconditionally. Two independent snapshots of the
same payment can therefore overwrite each other's committed amounts and
replay history. The durable contract removes that shape: mutations are
addressed by payment identity, planned against *current* stored state,
and committed atomically.

## Mandatory storage capabilities

A repository is usable for money movement only if it provides all of
these, listed in `getpaid_core.durable.MANDATORY_OPERATIONS`:

| Operation | Must do |
|-----------|---------|
| `get_payment_facts(payment_id)` | Return the payment's current committed financial facts |
| `reserve_operation(payment_id, intent)` | Commit a reservation against current facts; resume an identical intent instead of duplicating it |
| `claim_submission(payment_id, operation_id, *, expected_attempt, now, retry_until=None, idempotency_scope=None)` | Atomically grant one submission attempt and persist `SUBMITTING` before provider I/O; compare the attempt counter, preserve the original retry window |
| `apply_observation(payment_id, update)` | Apply a normalized observation to current state and return the committed plan |
| `record_operation_outcome(payment_id, operation_id, outcome)` | Commit an operation's outcome together with the financial facts it settles |
| `get_operation(payment_id, operation_id)` | Return one committed operation record |
| `list_unresolved_operations()` | Return the operations still holding a payment or awaiting reconciliation |
| `list_payments_requiring_reconciliation()` | Return the payments flagged for reconciliation, including those with no operation behind them |

Each of those calls is **one atomic boundary**. The payment's financial
facts, the affected operation record and the replay evidence commit
together or not at all. Core supplies the validation and transition rules
that run inside it — `plan_observation`, `plan_reservation`,
`plan_submission` and `plan_outcome` — so no adapter reimplements them.

`supports_durable_state(repository)` answers whether an adapter qualifies;
`missing_durable_operations(repository)` names what is absent.

Alongside the money, facts carry `reconciliation_required`. Evidence that
cannot be applied consistently — a provider event identity reused with
different content, for instance — sets it, and it commits with the facts.
Such evidence arrives on its own, with no command outstanding, so it is
enumerated through `list_payments_requiring_reconciliation()` rather than
through the operation list. Between the two, a restarted process finds
its outstanding work from stored state, without an exception, a log line
or a caller object.

## Optional, adapter-owned choices

Everything below is the adapter's decision, and core prescribes none of
it:

- **How atomicity is reached** — a transaction that reloads inside the
  boundary, a row lock, or a compare-and-set retry. Core only requires
  the result. When compare-and-set loses a race, raise
  `StateConflictError` and replan through `commit_semantic_transition`.
- **Storage engine and schema** — relational, document or JSON storage
  are all acceptable, with the same atomicity and durability guarantees.
  Core adds no ORM models, migrations, scheduler or database dependency.
- **Storage and archival mechanics** for operation and replay records —
  where they live and how they are archived, within the retention rules
  below.
- **Scheduling and operator escalation** — core exposes unresolved work
  through `list_unresolved_operations()`; who polls it, and who is
  allowed to resolve it, belongs to the application.

Two hard limits apply to those choices. **No atomic boundary may span
provider I/O**: the flow calls the provider outside it and applies the
normalized result afterwards. And **a process-local lock is not enough**:
it does not hold across workers, which is exactly the failure the
contract exists to prevent.

## Local conflicts are not resubmission

`StateConflictError` means another writer committed first, so the plan
was built on stale facts. It is a *local* failure with a local answer:
read current facts again, replan, commit — which is what
`commit_semantic_transition` retries for you.

It is never permission to send the financial command to the provider
again. Resubmission is governed by the provider's own idempotency
guarantee, and a retry loop around provider I/O is how one intent becomes
two transactions. The exception carries this distinction as
`retry_locally` and `provider_resubmission_allowed`.

## Refusing an unsupported adapter

`require_durable_state(repository, operation=...)` raises
`UnsupportedRepositoryError` when the repository cannot commit
atomically, naming exactly which operations are missing.
`DurablePaymentFlow` calls it at construction, so an unsupported adapter
is refused before any financial command can reach a provider.

There is deliberately no fallback to reading a snapshot and saving it
unconditionally, and no capability sniffing that quietly picks a weaker
path: an adapter that cannot make the guarantee must not move money in a
way that claims it.

## Durable operation dispatch

Use `DurablePaymentFlow.execute_operation(payment_id, intent, *, now)` for
`OperationType.PREPARE`, `CHARGE`, `RELEASE_LOCK`, `START_REFUND` and
`CANCEL_REFUND`. All five use the same reservation/submission/outcome boundary;
none invokes a legacy instance method as a fallback.

```python
from datetime import UTC, datetime
from decimal import Decimal
from getpaid_core.durable import OperationIntent, OperationType

result = await flow.execute_operation(
    payment_id,
    OperationIntent("capture-installment-1", OperationType.CHARGE,
                    amount=Decimal("30.00")),
    now=datetime.now(UTC),
)
# Read result.operation_id, result.outcome, result.snapshot and
# result.reconciliation_required, not a caller-owned Payment object.
```

The application supplies the operation ID, scoped to the payment. The same
ID and normalized request retrieves the original intent; changed type, amount
or parameters raises `OperationConflictError`. Use new IDs for deliberate
subsequent partial captures/refunds. An omitted amount is resolved once during
atomic reservation, after application validators, and never recalculated for
a same-ID retry. Nested request parameters are copied into immutable values;
nonfinite/unsupported values are refused. Mapping order and Decimal scale do
not distinguish otherwise identical requests.

Reservation preserves the starting captured/refunded/authorization totals,
concrete amount, normalized parameters, provider backend and operation-specific
idempotency key. The submission claim freezes the first submission time, key
scope and finite retry deadline. Adapters must persist **all** these fields,
not only the request digest. An older experimental durable record missing its
payload or submission history cannot be reconstructed by guessing: reconcile
it before enabling dispatch. Released 3.x migration still invents no intents.

One active intent holds a payment in `RESERVED`, `SUBMITTING`,
`PROVIDER_PENDING` or `UNKNOWN`. Duplicate calls do not submit independently;
unrelated commands conflict. Unknown is nonterminal, not rejection. Callbacks
and evidence recording continue while commands are blocked. Payment-level
reconciliation also blocks new submission rights for previously reserved work,
while retrieval and reconciliation remain available.

`OperationResult` carries the operation record and committed `snapshot`.
Acceptance does not move captured/refunded funds. Confirmed command effects
become cumulative observations using the reservation's starting totals, not
amounts added to a callback-updated snapshot. Never associate an external delta
with an intent just because their amounts match. A plugin unable to establish
trustworthy correlation must return `UNKNOWN` with `reconciliation_required`.

### Processor upgrade

Processors declare `operation_capabilities`, a mapping from `OperationType` to
`OperationCapabilities`. A missing entry means unsupported. Each entry declares:

- `idempotency_scope` and `idempotency_window` together, or neither. Document
  the provider account/endpoint namespace and immutable payload comparison
  rules. Keep the provider account/configuration stable for the intent's life.
- `lookup_semantics`: `UNSUPPORTED`, `AUTHORITATIVE`, or
  `AUTHORITATIVE_INCLUDING_ABSENCE`. Ordinary not-found is `UNKNOWN`. Only a
  documented conclusive guarantee excluding both past and later execution can
  normalize absence to `REJECTED`.

Implement the classmethods `submit_operation(operation, *, config)` and, when
lookup is declared, `lookup_operation(operation, *, config)`. Each returns
`OperationOutcome`. No caller-owned payment instance reaches these methods.
Construct the wire request only from the frozen `parameters`, `resolved_amount`
and `idempotency_key`; operational fields such as attempt counter and current
state are **not** request parameters. Currency, buyer/return information and
other provider request inputs belong in the explicitly reserved parameters,
not a later read of an order or payment. Do not independently retry requests
inside a plugin outside the declared guarantee.

Return operation-specific confirmed effects as `SUCCEEDED`, explicit refusal
as `REJECTED`, acceptance without settlement as `PROVIDER_PENDING`, and
uncertainty as `UNKNOWN`. Store safe handles in per-operation `correlation`;
never overwrite a payment-wide refund ID. Preparation can return `external_id`
for the payment's provider handle. Returned evidence is untrusted: no raw
payload or arbitrary result representation is added to logs or public errors.
Plugins must allowlist safe correlation handles rather than putting secrets
in them. Core cannot verify a plugin's real provider capability claims.

### Recovery and restricted mode

A timeout, crash before transmission, or expired worker lease leaves the
operation unresolved. Repeating `execute_operation` returns it without
resubmitting. The application invokes
`reconcile_operation(payment_id, operation_id, *, now, resubmit=False)` to
query declared authoritative evidence; there is no scheduler in core.

`resubmit=True` requests at most one new attempt, **after** lookup when
available. A failed lookup does not authorize retry. The attempt still needs
the original idempotency scope and a valid original window, also bounded by
the current declaration. Core leaves a full `provider_timeout` of headroom
(default 30 seconds) and counts query elapsed time against the deadline. A
provider without lookup can safely replay only under that same valid key
guarantee. An absent/expired guarantee, scope change, pending acceptance or
reconciliation flag refuses resubmission. Neither local record retention nor
a longer replacement capability declaration extends the original window.

`now` must be an accurate timezone-aware application clock shared consistently
across workers. Clock rollback is not a recovery strategy. Wrappers must bound
clock error conservatively; plugins must honor the request timeout and ensure
the provider receives retries within its declared guarantee. Core's asyncio
call timeout cannot make a blocking or noncompliant plugin safe.

If neither safe retry nor lookup is available, construction of the flow must
explicitly opt that operation into `restricted_operations=frozenset({...})`.
It may then submit **once**, retaining uncertainty after response loss. A
process crash before transmission can leave it stuck; time cannot distinguish
that from successful execution followed by lost acknowledgement. Operator
resolution and authorization remain integration responsibilities.

Pre-submission validation/conflict and storage failures remain exceptions.
After I/O, `OperationEvidenceError` identifies invalid/inapplicable normalized
evidence; `OperationPersistenceError` identifies a failed local outcome write.
Both expose only operation/payment identity, type and safe known correlation
and explicitly forbid blind provider resubmission. A failed final write leaves
the earlier durable submission record discoverable even if no further write
can succeed. Cancellation propagates and also leaves that anchor; this slice
creates no detached cleanup tasks. Richer post-response recovery and bounded
cancellation cleanup remain separate implementation work.

### Refund cancellation

Cancellation is a separate `CANCEL_REFUND` intent whose parameters name
`target_operation_id`. It is the sole exception to exclusive mutation: the
target must be a specific provider-pending refund with known correlation.
Reservation freezes that target correlation for submission. Other commands
remain blocked while either operation is uncertain. Confirmed cancellation
resolves the unexecuted target without decreasing refunded funds; a racing
settlement is preserved, not overwritten. A callback completing an operation
before a late acceptance response cannot downgrade terminal state.

## Who owns replay evidence

Replay bookkeeping is core-owned and lives in `ReplayRecord`, in storage
of its own. It is never an entry in `provider_data`, which is
unrestricted plugin metadata that every processor update merges into.
That separation is the whole point: while the two shared one mapping, a
payload carrying an `applied_event_ids` key could replace committed
history, or prepopulate an identity for an event that never arrived and
so suppress the genuine capture that followed it.

A `ReplayRecord` is compared on two things:

- **Scope** — `(payment_id, backend, event_identity)`. An event identity
  is unique within one payment at one provider, and the scope is read
  from the payment's *stored* facts, so a payload cannot nominate the
  context it is compared in.
- **Content** — a digest of the observation's core-owned semantic fields
  only. The provider payload is deliberately excluded, so a
  retransmission differing in transport noise still reads as the same
  event, while the same identity carrying different money does not.

Same scope and same content is a genuine duplicate: idempotent, nothing
applied. Same scope and *different* content is conflicting reuse: core
refuses to apply it and flags the payment for reconciliation rather than
letting a reused identity silently suppress a financial change.

`provider_data` keys that look like the old bookkeeping —
`getpaid_core.durable.LEGACY_REPLAY_METADATA_KEYS` names them — are kept
as ordinary readable metadata and never consulted. They are not rejected:
refusing an observation because of one of its metadata keys would hand
any provider payload the power to suppress a genuine financial change,
which is the failure this design exists to prevent.

What *is* rejected is metadata core cannot store: a `provider_data` that
is not a mapping, a non-string key, or a non-string event identity. The
refusal happens before anything is planned, so the adapter's boundary
commits nothing and both committed funds and committed history survive
it intact.

## Migrating released 3.x records

`getpaid_core.durable.migration` maps one stored legacy payment onto
durable facts. Core owns the mapping; the framework wrapper owns the
migration — reading rows, writing them back, its schema changes, and
sequencing the whole thing against the writer cutover below.

```python
from getpaid_core.durable import LegacyPaymentState, plan_migration

plan = plan_migration(LegacyPaymentState.from_payment(payment))
await my_repository.write_migrated(plan.facts)   # adapter-side write
```

Amounts, status and metadata are preserved exactly as stored. Two things
are never produced:

- **Replay evidence.** The released contract kept its applied-event list
  inside `provider_data`, where processor metadata could overwrite it.
  History that provider payloads had write access to cannot be certified
  after the fact, so it migrates as readable metadata, not as trusted
  evidence. A redelivery of one of those events therefore applies again;
  cumulative observations keep that harmless to the totals, but
  exactly-once delivery is no longer claimed for the payment's past.
- **Operation records.** The released contract recorded no operation
  identity, and inventing one would hand a historical retry a
  reservation it never had.

`plan_migration` reports what it established as `MigrationFinding`s:

| Finding | Meaning | Blocks mutation |
|---------|---------|-----------------|
| `AMBIGUOUS_FINANCIAL_RECORD` | The balances break the financial invariants, or the status is not one core defines | yes |
| `PENDING_OPERATION` | The record was left mid-operation, with no operation identity to resolve it against | yes |
| `UNPROMOTED_EVENT_HISTORY` | It carried a legacy applied-event list, kept readable and untrusted | no |

A blocking finding sets `reconciliation_required` on the migrated facts,
which is what `plan_reservation` enforces: the payment is **readable and
still takes observations** — callbacks and reconciliation continue — but
no *new* command may be reserved, and the attempt raises
`ReconciliationBlockedError`. Operations already reserved still resume
and still resolve, so blocking never strands outstanding work. An
unpromoted event history is deliberately not blocking: every legacy
payment that ever saw a callback carries one, and blocking them all would
migrate the whole estate into reconciliation.

Clearing the requirement is the application's reconciliation step, and an
adapter-side write like the migration itself. Core supplies the
invariants and the block; establishing what actually happened at the
provider, and recording who decided it, belongs above core.

A record whose balances are not finite `Decimal` money raises
`InvalidTransitionError` instead of migrating: there is nothing to
reconcile against, and the source data has to be repaired first.

## Coordinated writer cutover

Old unconditional-save workers must **stop before** new-contract writes
begin. The two flows must not write the same payment state: `PaymentFlow`
saves whatever object a caller handed it, so a single surviving legacy
worker can overwrite facts the durable path committed, and neither side
detects it.

The order that holds:

1. Stop every writer using `PaymentFlow` for the payments being migrated
   — web callbacks, pollers, scheduled jobs, management commands.
2. Migrate the records with `plan_migration`, writing the facts through
   the adapter.
3. Start the new writers on `DurablePaymentFlow`.

Do not run the two concurrently against one payment, and do not migrate
under live legacy traffic. Reads may continue throughout.

## Retention

Operation and replay records are compact and are kept for the payment's
**supported lifetime**, with no automatic expiry in this contract.
Deduplication that expires is deduplication that stops working, and an
operation record is the recovery anchor for an outcome that was never
established.

- **Sensitive provider evidence has its own retention.** Raw payloads and
  correlation captured for recovery are governed by the application's
  data-retention policy, separately from the compact records core needs.
- **Archival and deletion need an explicit policy**, and it must not turn
  an old retry into a fresh transaction: dropping the replay evidence for
  an event a provider may still redeliver, or the record of an operation
  a caller may still repeat, converts a duplicate into new money.
- **Local retention never extends a provider's idempotency window.**
  Keeping a record longer than the provider honours its key does not make
  resubmission safe.

## Proving an adapter conforms

`getpaid_core.durable.conformance` ships the checks an adapter must pass.
Supply a factory that returns your repository holding exactly the given
payment facts:

```python
from getpaid_core.durable import run_conformance_suite

async def factory(facts):
    repository = MyDjangoRepository()
    await repository.seed(facts)
    return repository

await run_conformance_suite(factory)   # raises ConformanceError on failure
```

The suite drives the repository through **independent concurrent
callers**, each holding its own detached snapshot, and reads state back
through the repository, never through an object it handed out earlier.
That distinction matters: a shared mutable fake passes races it should
fail, so it is not evidence. The checks cover a stale cumulative capture
racing a full one, a capture racing a refund, duplicate versus distinct
event identities, discovery of unresolved work, refusal of overlapping
commands, metadata that tries to forge or erase replay history, atomic
rejection of malformed metadata, and refusal of new commands on a payment
awaiting reconciliation.

Those callers are concurrent tasks, not separate processes. Passing
proves the semantic contract against your own storage; it proves nothing
about a live provider, and it cannot provoke every interleaving a real
deployment produces. An adapter whose atomicity rests on a database still
needs its own multi-process and isolation-level tests, plus tests for
migration and cutover.

`InMemoryDurableRepository` is a reference implementation to read and to
test the suite against. Its boundary is an in-process lock, which the ADR
rejects for real deployments; it is not production storage.

## The next-major adapter upgrade boundary

The contract ships in a **breaking major release**, and the cutover is an
explicit choice of flow class rather than something core infers from the
adapter:

| | `PaymentFlow` | `DurablePaymentFlow` |
|-|---------------|----------------------|
| Repository | `PaymentRepository` (released 3.x) | `DurablePaymentRepository`, checked at construction |
| `handle_callback()` | Updates and saves the caller's payment, returns `None` | Commits to current state, returns `ObservationPlan` |
| `fetch_and_update_status()` | Updates and saves the caller's payment, returns it | Commits to current state, returns `ObservationPlan` |
| `reserve_operation()` / `record_operation_outcome()` | — | Returns the committed record or plan |
| `execute_operation()` / `reconcile_operation()` | — | Durable command dispatch/recovery returns `OperationResult` |
| Atomicity across workers | None claimed | Guaranteed by the adapter's boundary |

`PaymentFlow` still makes none of the guarantees on this page, and nothing
in it consults the durable contract. Its transition rules are shared, so it
follows the same fact-based capture/release eligibility and status
projection as this layer; what it does not gain is atomicity.
`DurablePaymentFlow` never writes a caller-supplied object. A framework
wrapper may still accept a model instance for ergonomics, but only its
identity is authoritative; previously loaded objects may be stale, and
their financial fields are never written back.

Upgrading an integration therefore means: implement the mandatory
operations, pass the conformance suite, switch to `DurablePaymentFlow`,
and read the returned `ObservationPlan` instead of the payment object.
The ADR additionally requires a coordinated writer cutover — old
unconditional-save workers must stop before new-contract writes begin,
and the two flows must not write the same payment state.

## Not in this layer

These are deliberately absent here and tracked separately:

- **Legacy callback/PULL processor input** — these observation parsers still
  receive the caller's payment object. Durable command classmethods receive
  the frozen operation instead. Cross-channel callback correlation is separate
  work; do not infer it merely from the currently active operation.
- **Complete recovery policy** — richer post-response recovery evidence,
  cancellation-aware bounded cleanup and auditable operator-resolution APIs
  remain follow-on work. No recovery scheduler, database adapter or operator
  authorization system is implemented here.
- **Cross-channel observation reconciliation** — deciding what a stale
  or contradictory cumulative snapshot means when it arrives during or
  after a refund. Transitions here run the shared state engine, which
  already projects the ADR's status precedence and its partial-capture
  eligibility rules; correlating competing evidence is separate work.
