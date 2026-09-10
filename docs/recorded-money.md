# Recorded-money API

Status: **Upcoming-stack API, not the released 3.2 contract.** Pure semantics are
implemented in `getpaid_core.recorded_money`; storage must be supplied separately.
[ADR 0002](adr/0002-recorded-money-assertions.md) explains the distinction from
[irrevocable provider confirmation](adr/0001-durable-money-operations.md).

This API records an attributed assertion that money arrived or was actually
repaid outside a provider integration. It never sends money, invokes a processor,
reserves a provider operation or manufactures a provider event. Neither Dummy nor
a simulated gateway implements this contract.

## Initialization and source ownership

Create `getpaid_core.durable.PaymentFacts` with a stable payment identity, a
positive finite Decimal `amount_required`, and
`backend=RECORDED_MONEY_BACKEND` (`"getpaid.recorded_money"`). Start with zero
captured/refunded/authorized money, status `NEW` or `PREPARED`, empty histories,
`external_id=None`, empty `provider_data`, unknown fraud status, empty fraud
message, no observation conflicts and no reconciliation requirement.

The adapter must persist and enforce the immutable payment identity, backend and
required amount. Do not change a provider payment's backend to enter this API.
It cannot adopt nonzero legacy totals without retained recorded-money evidence;
provider operations/replay records, handles, metadata (even apparently benign
metadata), fraud evidence or reconciliation evidence cause refusal. No guessed
historical import, automatic cleanup, expiry, or source conversion is provided.
An empty history is valid only with a matching zero summary. A history that nets
to zero is not empty and must not be discarded.

## Public functions

All recorded-money names below are imported from `getpaid_core.recorded_money`.
Facts, provider operation records and provider replay records retain their
existing `getpaid_core.durable` types.

```python
plan_recorded_money(
    facts: PaymentFacts,
    history: Iterable[RecordedMoneyEntry],
    command: RecordedMoneyCommand,
    *,
    now: datetime,
    operations: Iterable[OperationRecord] = (),
    replay_records: Iterable[ReplayRecord] = (),
) -> RecordedMoneyPlan

project_recorded_money(
    history: Iterable[RecordedMoneyEntry],
    *,
    amount_required: Decimal | None = None,
) -> RecordedMoneyProjection
```

Supply **complete retained history in commit order**, never sorted by business
time (backdating is allowed). Supply complete provider histories too; they must
be empty. Defaults mean the integration has established their absence, not that
it may ignore existing records.

Projection validates identities, original evidence, corrections and financial
bounds at every retained prefix. Supplying `amount_required` also validates the
upper bound at every prefix; the planner always supplies it. Projection alone
has no current clock or stored summary to compare. The planner additionally
checks source purity, all audit timestamps against `now`, required amount,
authorization, summary totals and financial status before even an exact replay.
A mismatch is an error requiring investigation, never a repaired summary.

## Commands

`RecordedMoneyCommand` is a frozen, slotted dataclass:

| Field | Type / default | Contract |
|---|---|---|
| `command_id` | `str`, required | Stable identity scoped to this payment; preserve across retries |
| `kind` | `RecordedMoneyKind`, required | Typed enum, not a bare string |
| `actor` | `str`, required | Opaque authorized source/actor reference, interpreted by the application |
| `amount` | `Decimal \| None = None` | Positive finite amount for movements; must be absent for corrections |
| `occurred_at` | `datetime \| None = None` | Aware business time for movements; must be absent for corrections |
| `note` | `str \| None = None` | Optional short safe explanation |
| `evidence_reference` | `str \| None = None` | Required for repayments, optional for receipts, absent for corrections |
| `target_command_id` | `str \| None = None` | Required only for corrections; names an original receipt/repayment |
| `correction_reason` | `RecordingCorrectionReason \| None = None` | Required typed reason only for corrections |

`RecordedMoneyKind`: `RECEIPT="receipt"`, `REPAYMENT="repayment"`,
`CORRECTION="correction"`.

`RecordingCorrectionReason`: `MISTAKEN_ENTRY="mistaken_entry"`,
`INCORRECT_AMOUNT="incorrect_amount"`, `INCORRECT_TIMESTAMP="incorrect_timestamp"`,
`INCORRECT_REFERENCE="incorrect_reference"`, `DUPLICATE_ENTRY="duplicate_entry"`.
These classify mistaken assertions; none authorizes a real money movement.

Identifiers, actors, targets and evidence references must be nonblank printable
strings of at most 256 characters; notes have a 2000-character limit. Optional
absence is `None`, not an empty string. Whitespace is not stripped. No raw bank
payload or secret belongs in these fields: use opaque references to separately
controlled evidence. Core checks shapes, not whether arbitrary text is a secret
or an actor is authorized. Default command/entry/projection/plan reprs omit field
values, and errors do not echo raw input. Explicit serialization still exposes
the supplied fields and requires appropriate access control.

Finite Decimal values are not quantized or rounded to a currency's minor unit.
Summation does not depend on the caller's Decimal precision. The application
validates currency precision (for example PLN cents); core has no global 2dp rule.

## Retained records and returned plan

All these values are frozen, slotted dataclasses. Do not mutate or replace older
retained entries when a new plan arrives.

`RecordedMoneyEntry` fields:

- `payment_id: str`: the owning root.
- `command: RecordedMoneyCommand`: complete original command semantics.
- `recorded_at: datetime`: aware audit time supplied as planner `now` by the
  integration's clock, not by an untrusted client.
- `signed_amount: Decimal`: positive for receipt, negative for repayment; a
  correction is the exact inverse of its target's signed amount.
- `effective_at: datetime`: original business timestamp; corrections copy their
  target's timestamp rather than claiming money moved at correction time.
- `evidence_reference: str | None = None`: movement evidence, copied unchanged
  from the target for corrections, independently of the correction command.

`RecordedMoneyProjection` fields: `records` (complete ordered tuple),
`effective_receipts`, `effective_repayments` (tuples of uncorrected original
entries), `captured_funds`, `refunded_funds` (positive effective gross totals).
Correction entries stay in `records`, not in the effective movement lists. Use
these lists/totals rather than rebuilding correction arithmetic in an adapter.

`RecordedMoneyPlan` fields: `facts: PaymentFacts`, `entry: RecordedMoneyEntry`,
`projection: RecordedMoneyProjection`, `applied: bool`. With `applied=True`, entry
is new and facts are its new projection. With `applied=False`, entry is the
retained original and facts are unchanged current facts, not a historical
snapshot. No returned record is durable until its integration commits it.

## Replay and timestamps

Same payment plus same command ID plus equal normalized command content is an
exact replay. Decimal scales compare by value, and aware movement timestamps are
normalized to UTC (equivalent instants compare equally). Strings compare exactly;
changing actor, note, kind, amount, time, reference, target or reason conflicts.
Different IDs remain separate legitimate movements even when everything else
matches. Duplicate retained IDs are malformed history, not a history deduper.

Every call requires aware `now`; movement time must be aware and not later than
`now`. Backdated business time is accepted. Newly returned audit timestamps are
UTC. Retry clock time is excluded from command semantics and does not change the
original entry's `recorded_at`. Retrying an original entry after correction does
not resurrect it. Retrying a correction does not apply a second inverse. Current
history must still be valid, and none of its audit timestamps may be in the future
relative to the supplied clock. A correction cannot predate its target's audit
time. There is no system clock hidden inside core.

## Corrections, bounds and refusals

A correction must reference an earlier receipt or repayment, never another
correction or an already-corrected entry. It has its own identity, actor, reason
and audit timestamp. It derives amount, business time and original evidence;
callers cannot override those derived values. Replacing a mistaken amount means
full correction followed by a new truthful movement, not editing an entry.

Always `0 <= refunded <= captured <= amount_required` and authorization is zero.
A repayment cannot exceed unreturned receipts. A receipt correction that would
leave an actual repayment unfunded is refused without erasing evidence. A false
repayment can itself be corrected, retaining its original record. This is not a
general negative-balance/debt recovery API. Correcting then recording a larger
receipt may require a new payment root with its truthful required amount.

Status uses core's existing `fsm.project_payment_status`: partial/full receipts,
partial/full repayments, and corrections all reflect the effective totals. A
full mistaken receipt corrected to zero becomes `PREPARED`, not a fabricated
provider cancellation or refund. Ordinary provider captured/refunded observations
remain monotone; neither this API nor its correction entries change those rules.

`InvalidTransitionError` refuses malformed/source/history/time/bound violations;
`OperationConflictError` refuses changed semantics under a retained command ID.
These come from `getpaid_core.exceptions`. Refusal returns no plan and mutates no
input. Core does not clear evidence, silently skip bad entries, set a repair flag,
or decide how an operator should resolve a conflict.

## Adapter atomicity and rollout

Within one per-payment transaction/lock or equivalent compare-and-set boundary:

1. Address the stored root by identity; reload current facts and **all** recorded,
   provider operation and provider replay history. Never trust a caller snapshot.
2. Authenticate/authorize the actor at the integration boundary; validate the
   application's currency and policy rules. Pass the integration clock as `now`.
3. Invoke `plan_recorded_money` on that current state. On refusal, commit nothing.
4. On `applied=True`, append exactly `plan.entry` and update `plan.facts` together.
   Preserve all prior records unchanged; enforce unique `(payment_id, command_id)`
   and commit order. On replay, append nothing and return the retained plan.
5. Return only after commit. If a concurrency/version conflict loses the commit,
   reload and replan the same command; do not save the stale plan or mint a new ID.

The planner cannot detect every omitted history entry (for example, an omitted
receipt/correction pair whose totals cancel) or an adapter rewriting the source.
History completeness, immutable root fields and append-only storage are mandatory
integration guarantees, not properties proven by pure function tests. Process-local
locks are not durability. This API does not extend the existing provider repository
protocol or `MANDATORY_OPERATIONS`; a separate adapter-facing recorded-money method
must implement this boundary. Do not route it through provider dispatch.

Storage readers/writers must round-trip Decimal values, aware timestamps, enum
values, all command fields, record order and derived evidence without loss. Retain
history for the supported payment lifetime; no automatic expiry. Prevent older
writers, provider planners or unconditional model saves from writing these roots.
The adapter owns schema/migration/cutover, rollback and real concurrent-caller tests.
An existing durable provider repository alone does not prove recorded-money support.
Core tests prove semantics only, not database atomicity, live-provider safety or
end-to-end behavior. No network, ORM, allocation, Party, credit, consent, RBAC,
scheduler, bank importer or application UI is added here.
