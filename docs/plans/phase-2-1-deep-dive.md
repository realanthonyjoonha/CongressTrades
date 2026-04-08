# Phase 2.1 — Politician Deep-Dive Agent (CLOSED)

**Status:** Shipped as commit `76d0359`, pushed to `origin/main`.

## Context

Phase 0 closed with the backtest pipeline producing a real 3-politician core roster on 13,638 trades. Phase 2 split the remaining work into four agents (Data Maintenance, Daily Signal, Weekly Deep Research, Politician Deep-Dive). User chose Deep-Dive first because it has the smallest scope but still exercises every infrastructure piece — Python analytical driver, LLM prompt with web research, markdown→HTML→email delivery, on-demand CLI invocation. The pattern established here is the template for the other three agents.

## Scope

**In:**
1. Pull all trades for one politician from the SQLite DB
2. Compute hit rate, excess return vs SPY, sector concentration, committee alignment, timing patterns, top tickers, recent activity
3. Split member_direct from spouse from dependent trades (per spec)
4. Render a deterministic "research pack" markdown
5. LLM narrative layer reads the pack, does 15–25 web searches for current committee work + upcoming catalysts, synthesizes a single-politician report
6. Format → HTML → email to admin only (`anthonyjoonha@gmail.com`) via the new `--to` flag in `send_email.py`

**Out (deferred):**
- Time-varying historical committee membership (Phase 3)
- Live options chain via MMD (Daily Signal scope)
- Multi-politician comparison reports
- Legislative calendar API (covered by web search for v1)

## Architecture

Two-phase hybrid:
- **Phase A** (`scripts/deepdive.py`) — deterministic Python; queries SQLite, reuses `backtest.evaluate_politician()` internals so numbers match the backtest exactly. Writes a structured research pack to `outputs/tmp/`.
- **Phase B** (`prompts/deepdive.md` + `claude --print`) — narrative layer. Reads the pack as source of truth, never recomputes stats, web-searches for context, writes the report to stdout (which the runner captures as the deliverable).
- **Phase C** — `format_report.py` → HTML → `send_email.py --to admin`

The Python wrapper / runner template established here is reused for Phase 2.2 (Data Maintenance) and will be reused for 2.3+ as well.

## Files shipped

| File | Status | Notes |
|---|---|---|
| `scripts/deepdive.py` | NEW (~480 lines) | Analytical driver. Reuses `db.connect`, `db.query_trades`, `backtest.evaluate_politician`, `backtest.compute_excess_return`, `backtest.is_aligned`, `sectors.classify_ticker`, `ingest.canonical_politician_name`. |
| `prompts/deepdive.md` | NEW (~136 lines) | Narrative layer prompt. 11-section report spec with `SUBJECT:` header, 15–25 web search budget, no-recompute constraint. |
| `run-agent.sh deepdive` | NEW subcommand | 3-phase runner: Python driver → Claude subprocess → format_report → send_email --to. |
| `scripts/send_email.py` | MODIFIED | Added `--to` flag for admin-only delivery. Backward compatible. |
| `scripts/backtest.py` | MODIFIED | Added persistent `data/price_cache.json` (~10MB JSON, gitignored). 900× speedup on re-runs after first warm-up. |
| `.gitignore` | MODIFIED | Excludes `data/price_cache.json`. |

## Verification (already completed)

| Test | Result |
|---|---|
| Mark Green driver smoke (75 trades) | 0.83s warm; matches backtest exactly: 24 aligned, 87.5% hit rate, +9.8% excess |
| Josh Gottheimer scale test (2,012 trades, 238 unique tickers) | 15 min cold (cache priming) → 0.9s warm. Surfaces "high volume, no edge" — 95% aligned, 48.6% hit rate, -0.2% excess. NVDA is sole real winner (+21.6% over 11 buys). |
| Edge case: nonexistent politician | Clean exit 1 with LIKE-query suggestion; runner bails before Claude. |
| `format_report.py` HTML pipeline | Confirmed valid HTML output via stdin invocation. |
| Full live E2E with `claude --print` + email | **Pending user verification** — this session can't invoke Claude as a nested subprocess. Each component validated individually. |

## Known cascades into Phase 2.2+

- The `--to` flag in `send_email.py` becomes reusable for any agent that needs admin-only delivery (Data Maintenance failure alerts, etc.).
- The "thin Python wrapper + runner subcommand" pattern is the template for all remaining agents.
- The persistent price cache benefits any future agent that calls `backtest.compute_excess_return()`.
- The research-pack-then-narrative split is the template for Daily Signal's "compute-then-synthesize" flow.

## Outstanding for Phase 2.1

- User to run `./run-agent.sh deepdive "Mark Green"` end-to-end once to confirm Claude subprocess + email delivery work in production. This was deferred from the build session because nested Claude invocations aren't possible from inside a Claude session.
