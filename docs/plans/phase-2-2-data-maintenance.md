# Phase 2.2 — Data Maintenance Agent (Agent 1, "tracker")

## Context

The CongressTrades system currently has 13,638 trades in `data/congress.db` from the Phase 0 backfill, current through `disclosure_date = 2025-12-29`. New trades keep getting disclosed daily under the STOCK Act. Without a pipeline that pulls fresh filings each morning, the Deep-Dive agent (Phase 2.1) and the forthcoming Daily Signal agent (Phase 2.3) would analyze increasingly stale data.

Agent 2 closes that loop. It is **explicitly not** an LLM agent. It's a deterministic Python pipeline triggered by cron (or launchd — scheduling mechanism TBD as a separate task). No web research, no prompt, no Claude subprocess. Its job is to keep the database accurate and let downstream agents trust it.

**Why before Daily Signal?** Daily Signal scores "overnight disclosures" through a 4-stage pipeline — but overnight disclosures don't exist unless something pulls them. Building Daily Signal first would force a manual `./run-agent.sh ingest ...` before every test. Building Agent 2 first means every subsequent agent starts with a clean current database.

## Scope

The original spec for Agent 1 in `specs/07-agents.md` listed six work items. Three were stale or misplaced and have been excluded from Phase 2.2.

### IN scope
1. Pull fresh disclosures from House eFD via existing `scripts/ingest.py`
2. Reconcile + dedupe via existing `ingest.reconcile()` + `db.upsert_trade()` CONFLICT clause
3. Tag trades as `member_direct` / `spouse` / `dependent` (already handled by ingest)
4. Write to `trades` table (already handled by ingest)
5. Detect ingestion failures
6. Emit admin-only alert email (`anthonyjoonha@gmail.com` by default) on failure
7. Log stats to `outputs/logs/tracker_TIMESTAMP.log`

### OUT of scope (explicit exclusions)
- **Paper-trading mark-to-market** — paper trading was dropped during the brainstorm revision. The original spec wasn't updated; Phase 2.2 also fixes that spec.
- **Per-stock volatility metrics** (`hist_range_45d`, `realized_vol_60d`, `sector_vol_60d`) — these are only consumed by Daily Signal's Stage 2 priced-in diagnostic. Computing them in Agent 1 creates an orphan dependency on infrastructure that doesn't yet read them. Moved to Phase 2.3 alongside the Stage 2 code.
- **Capitol Trades scraping in Python** — current design defers to agent prompts. Adding a Python scraper would be significant new code; keep deferred.
- **Senate eFD ingestion** — Cloudflare session-cookie barrier; deferred in spec.
- **Cron / launchd wiring** — scheduling mechanism is a separate task. Phase 2.2 just makes the agent invokable manually via `./run-agent.sh tracker`.
- **Explicit price cache warming** — Agent 2 doesn't fetch prices, so there's nothing to warm. Cache stays warm via backtest + deepdive runs.

## Architecture

Thin Python wrapper around `scripts/ingest.py`:

```
run-agent.sh tracker
    │
    ▼
scripts/data_maintenance.py
    │
    ├─▶ db.get_latest_disclosure_date()           [new helper, scripts/db.py]
    │     │
    │     ▼
    │   Compute --since as (max_disclosure_date - 7 days)
    │   (or fallback to 30 days ago if DB is empty)
    │
    ├─▶ subprocess: scripts/ingest.py --source house-efd --year YYYY
    │                                  --parse-pdfs --max-pdfs 200
    │                                  --since YYYY-MM-DD
    │     │
    │     ▼
    │   Captures stdout, stderr, exit code
    │
    ├─▶ Parse "[ingest] persisted: N trades, M placeholders skipped"
    │
    ├─▶ Detect failure modes:
    │     - Non-zero exit code
    │     - "[source] FAILED" markers in stdout/stderr
    │     - Subprocess timeout (1 hour hard cap)
    │     - Missing stats line (script didn't complete)
    │
    ├─▶ On success: silent. Log stats. Exit 0.
    │
    └─▶ On failure:
          - Build HTML error report (reason + exit code + stderr tail)
          - Shell out to send_email.py --subject ... --html-file ... --to admin
          - Exit 1
```

**Why a Python wrapper instead of pure bash?** Three reasons: (1) dynamic `--since` computation needs a SQLite query; (2) error alerting needs HTML formatting that's awkward in bash; (3) consistency with the Phase 2.1 pattern.

The pipeline stays entirely deterministic: no Claude subprocess, no tokens, no rate limits. Cron-friendly.

## Dynamic `--since` strategy

**`max(disclosure_date) - 7 days`**, fallback to `today - 30 days` if DB is empty.

**Why a 7-day buffer?**
- House eFD filings can be added retroactively (members file late, the clerk's office reprocesses)
- Provides resilience against missed cron runs (3 days off → still picks up everything)
- Cheap insurance: re-fetching a week of already-known trades is fast and the upsert is idempotent

**Year parameter:** Default to current calendar year. January 1–15 rollover: also pull the prior year to catch late-December filings still trickling in.

## Files (final)

| File | Status | Notes |
|---|---|---|
| `scripts/db.py` | MODIFIED | New `get_latest_disclosure_date(conn, source=None)` helper + `db latest-disclosure [--source X]` CLI subcommand |
| `scripts/data_maintenance.py` | NEW (~370 lines) | Wrapper. CLI: `--since`, `--source`, `--max-pdfs`, `--no-parse-pdfs`, `--dry-run`, `--admin`, `--no-alert`, `--buffer-days`, `--timeout` |
| `run-agent.sh` | MODIFIED | New `tracker` subcommand that invokes `data_maintenance.py` with logging. Help text updated. |
| `specs/07-agents.md` | MODIFIED | Agent 1 section rewritten to reflect actual scope (paper trading + stock metrics removed). |
| `docs/plans/README.md` | NEW | Index for in-repo plan files. |
| `docs/plans/phase-2-1-deep-dive.md` | NEW | Closure summary for Phase 2.1. |
| `docs/plans/phase-2-2-data-maintenance.md` | NEW | This document. |

## Reused infrastructure (no copies, just imports)

- `db.connect()`, `db.get_latest_disclosure_date()` — `scripts/db.py`
- `scripts/ingest.py` (entire pipeline) — invoked as subprocess, no changes
- `scripts/send_email.py` (with `--to` flag from Phase 2.1) — invoked as subprocess

## CLI

```bash
# Full auto (dynamic --since from DB)
python3 scripts/data_maintenance.py

# Or via the runner
./run-agent.sh tracker

# Dry run (no DB writes; alerts still fire on failure)
./run-agent.sh tracker --dry-run

# Manual since override (skips DB lookup)
./run-agent.sh tracker --since 2026-03-20

# Override admin email for failure alerts
./run-agent.sh tracker --admin foo@bar.com

# Suppress failure alerts (for testing)
./run-agent.sh tracker --no-alert

# Source override
./run-agent.sh tracker --source finnhub
```

Exit codes:
- `0` — success (or dry-run completed cleanly)
- `1` — ingestion failed (admin alerted unless `--no-alert`)
- `2` — internal wrapper error (admin alerted unless `--no-alert`)

## Verification

Run after building:

1. **New DB helper smoke test**
   ```bash
   python3 scripts/db.py latest-disclosure
   python3 scripts/db.py latest-disclosure --source house_efd
   ```
   Expected: ISO date `2025-12-29` (current state) or whatever's most recent.

2. **Helper unit tests**
   ```bash
   python3 -c "from scripts.data_maintenance import compute_since_date, compute_years_to_pull; ..."
   ```
   Expected: `compute_since_date` returns `latest - 7d`, fallback returns 30d ago, `compute_years_to_pull` returns `[current_year]` normally and `[current_year, prior_year]` during Jan 1–15.

3. **Dry run**
   ```bash
   ./run-agent.sh tracker --dry-run
   ```
   Expected: Computes `--since`, runs ingest in `--dry-run` mode, prints stats, exits 0. Log at `outputs/logs/tracker_TIMESTAMP.log`.

4. **Manual since override**
   ```bash
   ./run-agent.sh tracker --since 2026-03-01 --dry-run
   ```
   Expected: Skips DB lookup, passes `2026-03-01` straight through.

5. **Simulated failure → admin alert**
   ```bash
   ./run-agent.sh tracker --since BOGUS --admin anthonyjoonha@gmail.com
   ```
   Expected: ingest.py rejects bogus date, wrapper catches non-zero exit, formats HTML error report, sends via send_email.py. Exit 1. Email arrives at admin with subject `[ERROR] Data Maintenance — YYYY`.

6. **Empty-DB fallback** (synthetic)
   ```bash
   python3 -c "
   import sqlite3
   from scripts.data_maintenance import compute_since_date
   conn = sqlite3.connect(':memory:')
   conn.row_factory = sqlite3.Row
   conn.execute('CREATE TABLE trades (disclosure_date TEXT, source TEXT)')
   print(compute_since_date(conn))
   "
   ```
   Expected: Today minus 30 days.

7. **Real run** (after user confirmation)
   ```bash
   ./run-agent.sh tracker
   ```
   Expected: Pulls fresh filings, adds new trades, exits silently. Check `git diff --stat data/congress.db` afterwards — should show changes.

8. **Dispatch portability check**
   ```bash
   ls docs/plans/
   ```
   Expected: `README.md`, `phase-2-1-deep-dive.md`, `phase-2-2-data-maintenance.md` all present and committed to `main`.

## Edge cases handled

- Empty DB on first run → 30-day fallback
- DB has trades but `disclosure_date` is NULL → 30-day fallback
- DB has trades with malformed `disclosure_date` → 30-day fallback
- ingest.py exits non-zero → captured, alert sent, exit 1
- ingest.py prints `[house-efd] FAILED: ...` but exits 0 → still detected as failure via the `FAILED_RE` regex (ingest.py has a known soft-fail mode)
- ingest.py hangs → 1-hour subprocess timeout, treated as failure
- send_email.py itself fails during alert dispatch → logged to stderr, doesn't mask original failure, runner still exits 1
- `--no-alert` passed → failure logs to stderr but no email
- January 1–15 rollover → both current and prior year pulled
- Stats line missing from ingest output → treated as silent failure

## Out of scope (hard exclusions)

Not built in Phase 2.2:
- Cron / launchd wiring (separate task)
- Capitol Trades Python scraper (deferred to prompts)
- Senate eFD (deferred — Cloudflare)
- Stock metrics computation (Phase 2.3)
- Paper trading mark-to-market (dropped permanently)
- Retry logic beyond what `ingest.http_get()` already does
- Monitoring / Prometheus metrics

## Known cascades

After Phase 2.2:
- **Phase 2.3 Daily Signal** can assume the DB is fresh every morning. No more "run ingest first" prereq in its prompt.
- **`get_latest_disclosure_date()`** is reusable for staleness warnings in Deep-Dive and other agents.
- **Error-alert pattern** in `data_maintenance.py` becomes the template for Daily Signal + Weekly Deep failure modes.
- **`docs/plans/`** unlocks Dispatch-portable development for all future phases.
- **`--to` flag in `send_email.py`** gets its second consumer, validating the abstraction.

## Outstanding for Phase 2.2

- After local smoke tests pass, run `./run-agent.sh tracker --dry-run` once to confirm the runner pipeline.
- Optionally run a real `./run-agent.sh tracker` to see how many new trades have been disclosed since 2025-12-29 (the current latest).
- Schedule the cron / launchd job (separate task — see `CLAUDE.md`).
