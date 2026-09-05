# Domain Docs

This repository is single-context. Sibling repositories in the parent ecosystem
workspace are independent projects, not contexts in a monorepo.

## Before exploring, read these

- `CONTEXT.md` at the repository root: the domain glossary.
- Relevant decisions in `docs/adr/` at the repository root.

If a document or the ADR directory does not exist, proceed silently. Create domain
artifacts lazily when terminology or decisions are actually resolved; do not
create placeholder ADRs as part of setup.

## File structure

```text
CONTEXT.md
docs/adr/
src/getpaid_core/
```

## This file is the authority on where the glossary lives

When a skill refers to the project's domain glossary, use `CONTEXT.md`.
`docs/research/` contains supporting research, not an automatic substitute for a
recorded decision. The glossary includes recurring-agreement terminology that
must not be mistaken for proof that all corresponding features are implemented.

## Use the glossary's vocabulary

Use defined terms in issue titles, tests, documentation, and design discussions.
Do not substitute synonyms that change the domain meaning. If a needed term is
missing, distinguish a genuine glossary gap from terminology invented by the task.

## Flag ADR conflicts

Surface conflicts with existing decisions explicitly rather than silently
changing the domain model. Record new decisions through the domain-modeling
workflow when the decision has actually been made.
