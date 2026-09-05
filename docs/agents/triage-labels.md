# Triage labels

Canonical roles map to identically named tracker labels in this repository.
The factory has its own fixed label vocabulary; do not add `tracker.labels` to
`.pi/factory.json`.

## State roles

| Canonical role | Label in our tracker | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | Needs evaluation |
| `needs-info` | `needs-info` | Waiting for reporter information |
| `ready-for-agent` | `ready-for-agent` | Fully specified and ready for an agent |
| `ready-for-human` | `ready-for-human` | Requires human judgment or implementation |
| `wontfix` | `wontfix` | Will not be actioned |

## Category roles

| Canonical role | Label in our tracker | Meaning |
| --- | --- | --- |
| `bug` | `bug` | Existing behavior is wrong |
| `enhancement` | `enhancement` | New capability or improvement |

Resolve label roles through these tables rather than inventing alternatives.
Create missing labels before applying them, after checking the selected tracker.
Use the configured tracker's CLI with its explicit repository and authentication
profile. If tracker bindings are unavailable, require setup rather than guessing.

## Workflow labels

`workflow:implement` marks build-ready implementation work for `/implement`.
Workflow and state are separate: also use `ready-for-agent` or `ready-for-human`
to state who can execute it. A priority in an issue title is not a readiness label.

## Wayfinder labels

Wayfinding uses `wayfinder:map` and `wayfinder:<type>`, where type is `research`,
`prototype`, `grilling`, or `task`. These route decision work; they do not by
themselves mark implementation tickets ready for automation.
