# Implementation Plans

This directory holds the canonical, in-repo version of each implementation plan for the CongressTrades project. Plans are committed to git so they're available from any environment — including Dispatch (mobile Claude Code) sessions that don't have access to the local `~/.claude/plans/` directory on the developer's MacBook.

## Convention

- One file per phase: `phase-X-Y-short-name.md`
- Each plan follows the same structure: Context → Scope → Architecture → Files → Verification → Out of scope → Cascades
- Closed phases stay in this directory as historical record; they are not deleted after the work ships

## Phases

### Closed
- [`phase-2-1-deep-dive.md`](phase-2-1-deep-dive.md) — Politician Deep-Dive Agent (Agent 4). Shipped as commit `76d0359`.

### Active
- [`phase-2-2-data-maintenance.md`](phase-2-2-data-maintenance.md) — Data Maintenance Agent (Agent 1, "tracker"). Implementation in progress.

### Planned
- Phase 2.3 — Daily Signal Agent (Agent 3)
- Phase 2.4 — Weekly Deep Research Agent (Agent 5)

## Working from Dispatch

When resuming this project from a Dispatch session (or any non-Mac environment):

1. Clone `realanthonyjoonha/CongressTrades` from GitHub
2. Open the active plan in `docs/plans/`
3. Read `CLAUDE.md` for project conventions
4. Continue from the verification section of the active plan

The Mac's local `~/.claude/plans/` directory is NOT synced to the repo and is not necessary to resume work.
