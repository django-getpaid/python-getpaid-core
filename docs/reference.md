# API Reference

## Enums

```{eval-rst}
.. automodule:: getpaid_core.enums
   :members:
   :undoc-members:
```

## Processor

```{eval-rst}
.. automodule:: getpaid_core.processor
   :members:
   :undoc-members:
```

## State Engine

```{eval-rst}
.. automodule:: getpaid_core.fsm
   :members:
   :undoc-members:
```

## Protocols

```{eval-rst}
.. automodule:: getpaid_core.protocols
   :members:
   :undoc-members:
```

## Types

```{eval-rst}
.. automodule:: getpaid_core.types
   :members:
   :undoc-members:
```

## Durable Provider Contract

```{eval-rst}
.. automodule:: getpaid_core.durable.provider
   :members:
   :undoc-members:
```

## Durable Records

The following extends the **unreleased next-major durable contract**, not the
released `PaymentFlow` interface. Types are defined in
`getpaid_core.durable.records`.

### PaymentObservation

`PaymentObservation` subclasses `PaymentUpdate`, retaining its payment/fraud
events, cumulative financial fields, event identity and metadata. It adds:

| Field | Type | Default | Contract |
|-------|------|---------|----------|
| `operation_id` | `str \| None` | `None` | Nonempty merchant operation identity from an authenticated provider echo; requires `outcome` |
| `outcome` | `OperationOutcome \| None` | `None` | Operation-specific normalized evidence; absent an operation ID, `correlation` must uniquely match a retained handle in the payment/backend context |
| `delta_only` | `bool` | `False` | Actual boolean; financial delta fields are never added directly to current totals |
| `cancellation_scope` | `OperationType \| None` | `None` | Only `OperationType.RELEASE_LOCK`, paired with `PaymentEvent.LOCK_RELEASED`, for explicit authorization-only release |

Equal amounts, aggregate totals and the currently active intent are not
correlation. A correlated outcome uses frozen operation history to establish
cumulative money; uncorrelated outcome/delta evidence requires reconciliation.
An aggregate observation alone never completes an operation. Scoped release
cannot refund funds; correlated refund cancellation uses the reserved explicit
target, not an arbitrary active refund.

### ObservationConflict and ObservationPlan

`PaymentFacts.observation_conflicts` defaults to `()` and retains immutable
`ObservationConflict(event_identity, semantic_content, reason)` records.
`event_identity` may be `None`; `semantic_content` is allowlisted normalized JSON,
excluding raw provider metadata. Preserve these compact records without automatic
expiry, including when an observation is not financially applied.

`ObservationPlan` contains `facts`, `replay_record`, `applied`, and
`operations: tuple[OperationRecord, ...] = ()`. Every returned operation must commit
atomically with facts and replay evidence. `applied=False` does not mean facts
are unchanged: retained conflicts and reconciliation flags still require a write.

```{eval-rst}
.. automodule:: getpaid_core.durable.records
   :members:
   :undoc-members:
```

## Durable Repository

```{eval-rst}
.. automodule:: getpaid_core.durable.repository
   :members:
   :undoc-members:
```

## Durable Rules

`plan_observation(facts, replay_log, update, *, operations=())` must receive the
payment's **complete retained operation history**, read with current facts and
replay records inside the same atomic boundary. It applies independently supplied
captured/refunded totals, preserves refund progress on equal/lower captures, and
records valid increased capture during/after refund with reconciliation required.
It never initiates compensation. Ambiguous cancellation retains evidence rather
than cancelling an arbitrary pending refund.

Finite financial-bound violations are retained in
`PaymentFacts.observation_conflicts`; `plan_outcome(..., operations=...)` similarly
retains finite impossible settlement amounts and derived cumulative-bound
violations in `OperationRecord.conflicting_outcomes`. Disputed operation money
does not replace established balances or the prior operation state. Malformed
types, nonfinite values and truly impossible lifecycle evidence still raise
atomically. New reservations and submission rights are blocked while payment
reconciliation is required; evidence recording continues.

Observation digests now use JSON field boundaries instead of delimiters.
**Existing pre-release durable replay digests require coordinated offline
re-keying from original normalized evidence, or mutation-blocking/reconciliation
if that evidence is unavailable. Never blindly discard old history.** See
[pre-release upgrade requirements](durable-storage.md#upgrading-pre-release-durable-records)
for adapter persistence changes; the released 3.x migration remains a separate path.
These core mechanics do not certify real adapters/providers or completion of the
whole ADR.

```{eval-rst}
.. automodule:: getpaid_core.durable.rules
   :members:
   :undoc-members:
```

## Durable Migration

```{eval-rst}
.. automodule:: getpaid_core.durable.migration
   :members:
   :undoc-members:
```

## Durable Flow

```{eval-rst}
.. automodule:: getpaid_core.durable.flow
   :members:
   :undoc-members:
```

## Adapter Conformance

```{eval-rst}
.. automodule:: getpaid_core.durable.conformance
   :members:
   :undoc-members:
```

```{eval-rst}
.. automodule:: getpaid_core.durable.memory
   :members:
   :undoc-members:
```

## Exceptions

```{eval-rst}
.. automodule:: getpaid_core.exceptions
   :members:
   :undoc-members:
   :show-inheritance:
```

## Registry

```{eval-rst}
.. automodule:: getpaid_core.registry
   :members:
   :undoc-members:
```
