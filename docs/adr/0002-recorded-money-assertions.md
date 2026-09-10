# Recorded money is an audited assertion, not provider confirmation

Status: **Accepted for the upcoming stack; not a released 3.2 capability.**

Some payments arrive or are repaid outside a provider integration. Treating a
human recording as a simulated gateway confirmation would make correcting a
mistake indistinguishable from undoing real settled money. We instead expose a
separate pure recorded-money planner with attributed, immutable records and a
source identity fixed at initialization.

## Decision and boundary

Only newly initialized recorded-money roots start this history, with zero
captured/refunded/authorized money and no provider operations, replay records,
handles, metadata or reconciliation evidence. Adapters retain complete ordered
history, keep the source and required amount immutable, and commit each new
record and projected facts atomically against current stored state. Core does
not supply storage or claim that these pure plans prove durable execution.

A receipt or repayment asserts an actual external movement. A correction
withdraws exactly one mistaken assertion through its signed inverse, keeping
the original business timestamp and evidence reference. The correction's actor,
reason and audit timestamp are new evidence; the original record is untouched.
Correction-of-correction and a second correction of the same entry are refused.
The effective-record projection owns the totals so adapters need no separate
financial model.

This is a narrow source-scoped exception to monotone monetary summaries, **not a
relaxation of [ADR 0001](0001-durable-money-operations.md)** for providers.
Provider-confirmed captured and refunded funds remain irrevocable under ordinary
observation and operation planners. A negative capture or fabricated refund
confirmation must never implement a recording correction. Provider operation,
replay and reconciliation mechanisms remain unchanged.

Financial bounds remain `0 <= refunded <= captured <= required` with no
remaining authorization. Correcting a receipt cannot leave an actual repayment
unfunded. Correct a repayment only when its assertion was false, never to erase
money that really left. A replacement requires a correction plus a new truthful
movement, possibly a new root with a larger required amount. This deliberately
excludes general debt recovery and compensating transactions.

## Consequences

History grows for the root's lifetime. Exact command replay depends on retained
identity and normalized content, not retry time or equal amounts. Source/history
conflicts refuse rather than guess repairs; unexplained legacy nonzero balances
cannot be imported. No planner can prove an adapter supplied history it omitted,
or authenticate the actor/reference it was given.

The framework integration owns transactional persistence and concurrency; the
application owns authorization, identity, currency precision, allocation and
business policy. This API adds no provider simulator, ORM, scheduler, bank import
or release. See the [API and adapter contract](../recorded-money.md).
