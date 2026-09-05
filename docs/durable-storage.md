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
| `apply_observation(payment_id, update)` | Apply a normalized observation to current state and return the committed plan |
| `record_operation_outcome(payment_id, operation_id, outcome)` | Commit an operation's outcome together with the financial facts it settles |
| `get_operation(payment_id, operation_id)` | Return one committed operation record |
| `list_unresolved_operations()` | Return the operations still holding a payment or awaiting reconciliation |

Each of those calls is **one atomic boundary**. The payment's financial
facts, the affected operation record and the replay evidence commit
together or not at all. Core supplies the validation and transition rules
that run inside it — `plan_observation`, `plan_reservation` and
`plan_outcome` — so no adapter reimplements them.

`supports_durable_state(repository)` answers whether an adapter qualifies;
`missing_durable_operations(repository)` names what is absent.

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
- **Retention and archival** of operation and replay records, subject to
  the ADR's rule that archival must not turn an old retry into a fresh
  transaction.
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
atomically. Call it **before** provider I/O for any money-moving
operation, as `PaymentFlow.reserve_operation()` does. There is
deliberately no fallback to reading a snapshot and saving it
unconditionally: an adapter that cannot make the guarantee must not move
money in a way that claims it.

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

The suite drives the repository through **independent workers** and reads
state back through the repository, never through an object it handed out
earlier. That distinction matters: a shared mutable fake passes races it
should fail, so it is not evidence. The checks cover a stale cumulative
capture racing a full one, a capture racing a refund, duplicate versus
distinct event identities, discovery of unresolved work, and refusal of
overlapping commands.

Passing proves the semantic contract against your own storage. It proves
nothing about a live provider, and nothing about behaviour under a real
database's isolation level that the suite does not exercise. Framework
wrappers still need their own tests for transactional behaviour and for
migration and cutover.

`InMemoryDurableRepository` is a reference implementation to read and to
test the suite against. Its boundary is an in-process lock, which the ADR
rejects for real deployments; it is not production storage.

## The next-major adapter upgrade boundary

The contract ships in a **breaking major release**. Until an adapter
provides the mandatory operations, `PaymentFlow` keeps the released 3.x
behaviour for callbacks and polling — the caller's object is updated and
saved — and that path makes none of the guarantees on this page. As soon
as the repository qualifies, both route through the durable boundary and
return the committed plan instead:

| Call | 3.x repository | Durable repository |
|------|----------------|--------------------|
| `handle_callback()` | Updates and saves the caller's payment, returns `None` | Commits to current state, returns `ObservationPlan` |
| `fetch_and_update_status()` | Updates and saves the caller's payment, returns it | Commits to current state, returns `ObservationPlan` |
| `reserve_operation()` | `UnsupportedRepositoryError` | Returns the committed `OperationRecord` |
| `record_operation_outcome()` | `UnsupportedRepositoryError` | Returns the committed `OutcomePlan` |

A framework wrapper may still accept a model instance for ergonomics, but
only its identity is authoritative. Previously loaded objects may be
stale, and their financial fields are never written back.

Upgrading an integration therefore means: implement the mandatory
operations, pass the conformance suite, and stop reading the return value
of `handle_callback()`/`fetch_and_update_status()` as a payment object.
The ADR additionally requires a coordinated writer cutover — old
unconditional-save workers must stop before new-contract writes begin.

## Not in this layer

These are deliberately absent here and tracked separately:

- **Command dispatch and provider idempotency** — wiring `charge()`,
  `start_refund()` and friends through reservations, submission rights
  and provider idempotency keys. This layer supplies the reservation and
  outcome mechanics those will use.
- **Ownership and migration of existing payment metadata** — replay
  evidence is core-owned as `ReplayRecord` from here on, but migrating
  the historical `provider_data["applied_event_ids"]` lists is separate
  work.
- **The revised public status precedence** — the ADR's rules for
  refund-in-progress, partial refunds and authorization release change
  what a payment status projects. Transitions here still run the released
  state engine, so an authorization release with captured funds still
  reports the 3.x status.
