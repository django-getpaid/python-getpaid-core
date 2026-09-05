# AGENTS.md

This is the independent `python-getpaid-core` repository, not the parent ecosystem
workspace. Keep Git operations and changes inside this repository.
`CLAUDE.md` is a relative symlink to this file; maintain instructions here only.

## Agent skills

### Issue tracker

Agent work and human/community intake are separate surfaces. Use the explicit
tracker bindings supplied in session context or the local configuration.
See `docs/agents/issue-tracker.md` when that local file is available.
If neither source supplies the bindings, require `/setup-project-skills` before
tracker operations. Never infer the work tracker from `origin` or fall back to
intake when a work-tracker lookup fails.

### Triage labels

Use the default canonical state/category labels and `workflow:implement` routing.
See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: the glossary is `CONTEXT.md`; decisions live in `docs/adr/`.
See `docs/agents/domain.md`.

### Software factory

The opt-in factory policy is `.pi/factory.json`. Final merge remains manual.
`factory doctor` validates configuration and reports state without launching workers
or running checks; `factory doctor --baseline` explicitly runs checks.
`factory start <ticket-or-parent>` starts automation and detaches into a Herdr pane
by default; use `--foreground` to run the controller in the invoking terminal.
Do not start automation merely because this configuration exists.

## Local configuration and publication

`.pi/factory.json`, `docs/agents/issue-tracker.md`, and `.unforget/` are ignored
local state. Do not force-add them. Keep shared instructions and documentation
free of private tracker addresses, operator bindings, and local infrastructure
details; do not copy those details into commit messages or public artifacts.
Ignored files require a separate private backup and setup in another checkout.

Factory clones do not inherit ignored files. Supply required private instructions
through the local factory policy's explicit `worker.contextFile` mechanism;
context already supplied that way does not require recreating a local tracker file
inside a worker checkout. Factory workers leave tracker operations to the controller.

## Mandatory commands

Run from this repository root:

```sh
uv run pytest --ignore=tests/test_benchmarks.py --tb=short
uv run ruff check .
uv run ty check src
```

These are the factory's required checks; keep this list aligned with
`.pi/factory.json`. Use targeted tests during implementation. Benchmarks and the
network-dependent dependency audit are outside factory checks; the existing CI
audit remains unchanged. No live-provider or E2E assurance follows from these checks.
