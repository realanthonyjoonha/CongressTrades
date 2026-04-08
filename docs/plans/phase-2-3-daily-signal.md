# Phase 2.3 — Daily Signal Agent (Agent 3, "daily")

## Context

Phase 0 produced the data foundation (13,638 trades). Phase 2.1 shipped the Deep-Dive agent. Phase 2.2 shipped the silent Data Maintenance pipeline. Phase 2.3 is **the actual product** — the daily research report that turns disclosed congressional trades into actionable options-trade ideas.

This is the agent the entire project exists to deliver. Every prior phase was infrastructure. Phase 2.3 is where the signal generation happens.

The agent runs every weekday morning after Data Maintenance. It picks up trades disclosed in the trailing 24–72 hours (catches weekend rollover), runs each through the 4-stage scoring pipeline defined in `specs/02-pipeline.md`, persists the full diagnostic telemetry, and emits a daily digest email with explicit signal tiers and conceptual options recommendations. If any single trade scores **STRONG**, an instant alert email fires with `[STRONG SIGNAL]` in the subject.

## Goals

1. Score every overnight disclosed trade through Stages 1–4 deterministically
2. Persist the full pipeline trace (every threshold, every check that fired) to `trade_diagnostics` for the future feedback loop (specs/04)
3. Produce a daily digest that's a 5-minute scan: TL;DR + STRONG plays + BASE plays + commentary on noteworthy MODERATE / SKIPs
4. Surface STRONG-tier plays via a separate instant alert email so high-conviction trades aren't buried in the digest
5. Establish the pipeline architecture in a way that Phase 2.3.5 (real Greeks via MMD) and Phase 3 (feedback loop) drop in without rework

## Scope

### IN scope

**4-stage pipeline (deterministic Python):**
1. **Stage 1 — Alignment Multiplier.** Per spec: 1.0x committee-aligned, 0.7x leadership floor, 0.5x mega-cap-tangential, 0.3x off-committee. Multiplier, not hard filter.
2. **Stage 2 Lite — Opportunity Window Diagnostic.** 5 of 8 checks computable from yfinance alone. A3 (IV expansion) and B1 (earnings pass-through) are MMD-deferred. Same 0/2/4 thresholds — v1 is more conservative (under-flag).
3. **Stage 3 — Forward Catalyst Identification.** LLM Phase B with 5–10 web searches per trade, 80 search hard cap.
4. **Stage 4 — Clustering Check.** SQL query for other roster members on same ticker within 30-day `k_cluster_window`. Spouse trades count at 0.5x weight.

**Final tier assembly + outputs:**
- STRONG / BASE / MODERATE / SKIP via `pipeline.assemble_final_tier()`
- `trade_diagnostics` row per scored trade with full pipeline trace
- `recommendations` row for STRONG and BASE tiers
- Daily digest email
- Instant `[STRONG SIGNAL]` alert if any STRONG trade
- New `config/email-distro-daily.json` (admin-only by default)

**Conceptual options layer (no MMD):**
- Per-tier delta band (STRONG 0.40–0.55, BASE 0.40–0.50, MODERATE no play)
- DTE = catalyst date + 14d buffer if Stage 3 found a catalyst, else 6–12 weeks
- LEAPS branch for multi-quarter structural theses (delta 0.65–0.80, 12–18mo DTE)
- Bear case as 1–2 sentence prose
- No specific strikes, no Greeks, no bid/ask

### OUT of scope (Phase 2.3.5+)

- MMD MCP integration → Phase 2.3.5
- `stock_metrics` table population (cached) → Phase 2.3.5; v1 computes inline
- A3 IV expansion + B1 earnings pass-through checks → Phase 2.3.5
- Real options chains, Greeks, specific strikes → Phase 2.3.5
- Spouse trade media-coverage detection → out of v1
- Paper trading mark-to-market → permanently dropped
- Feedback loop / parameter recalibration → Phase 3
- Pipeline backfill against historical trades → separate task
- Cron / launchd wiring → separate task

## Architecture

Three-phase split, mirrors Phase 2.1 Deep-Dive:

```
run-agent.sh daily
    │
    ▼
Phase A: scripts/daily_signal.py
    ├─ db.get_overnight_trades(lookback=3)        ← idempotent (excludes already-scored)
    ├─ FOR EACH trade:
    │     ├─ pipeline.compute_alignment_multiplier()   [Stage 1]
    │     ├─ pipeline.compute_owd_score()              [Stage 2 Lite — 5 checks]
    │     │   └─ stock_metrics.compute_metrics_for_trade()  (yfinance)
    │     └─ pipeline.find_clustering()                [Stage 4]
    ├─ db.upsert_trade_diagnostics() per trade
    ├─ db.update_trade_pipeline_columns()              (denormalized cols on trades)
    ├─ select_for_llm(top 20 by alignment × cluster)
    └─ render_research_pack() → outputs/tmp/daily_*_pack.md
    │
    ▼
Phase B: prompts/daily_signal.md + claude --print
    ├─ Read research pack
    ├─ FOR EACH selected trade:
    │     └─ 5-10 web searches for forward catalyst (90d window)
    ├─ Synthesize daily digest narrative
    └─ Emit fenced JSON block with Stage 3 results + final tiers
    │
    ▼
Phase C: run-agent.sh (bash)
    ├─ grep SUBJECT line from narrative
    ├─ format_report.py → HTML
    ├─ send_email.py --distro config/email-distro-daily.json
    └─ IF subject mentions "N STRONG" with N>0:
          send_email.py --subject "[STRONG SIGNAL] N plays - DATE"
```

## Files shipped

| File | Status | Notes |
|---|---|---|
| `scripts/stock_metrics.py` | NEW (~370 lines) | yfinance metric computation: hist_range_45d, realized_vol_60d, RSI, volume spike, sector ETF metrics. Reuses backtest's persistent price cache. NaN-filtered (yfinance returns NaN for partial bars). MMD-deferred fields stubbed as None. |
| `scripts/pipeline.py` | NEW (~520 lines) | 4-stage scoring engine. `compute_alignment_multiplier`, `compute_owd_score`, `find_clustering`, `assemble_final_tier`, `score_trade`. Direction-aware OWD checks (A1/A2 only fire when move is in trader's favor — bug discovered during smoke testing). |
| `scripts/options_concept.py` | NEW (~210 lines) | Conceptual options helper. `concept_for_trade()` returns structure / delta target / DTE window / rationale. Catalyst-driven DTE, LEAPS branch for multi-quarter theses. No MMD. |
| `scripts/daily_signal.py` | NEW (~370 lines) | Phase A driver. fetch_overnight, score_all, persist_diagnostics, select_for_llm, render_research_pack. |
| `prompts/daily_signal.md` | NEW (~200 lines) | Phase B prompt. 9-section report structure with required `SUBJECT:` header and required final fenced JSON block for round-tripping Stage 3 results to the DB. |
| `config/email-distro-daily.json` | NEW | Admin-only by default. User edits before going live. |
| `scripts/db.py` | MODIFIED | New `get_overnight_trades()`, `upsert_trade_diagnostics()`, `upsert_recommendation()`, `update_trade_pipeline_columns()` helpers. |
| `scripts/send_email.py` | MODIFIED | New `--distro` flag for alternate distro JSON files. Daily Signal uses `config/email-distro-daily.json`; Deep-Dive still uses `--to` for admin-only. |
| `run-agent.sh` | MODIFIED | New `daily` subcommand. 3-phase pattern (Python driver → Claude subprocess → format/email). STRONG-tier instant alert as second email. Help text updated. |
| `specs/07-agents.md` | MODIFIED | Agent 2 section rewritten to reflect actual scope. Removed stale "spouse media coverage detection" item. |
| `docs/plans/phase-2-3-daily-signal.md` | NEW | This document. |

## CLI

```bash
# Full auto (3-day lookback, dynamic since latest disclosure)
./run-agent.sh daily

# Custom lookback window
./run-agent.sh daily --lookback 7

# Limit trades sent to LLM Phase B
./run-agent.sh daily --limit-trades 10

# Dry run (no DB writes, but still calls Claude)
./run-agent.sh daily --dry-run

# Phase A only (no Claude — fast smoke test)
python3 scripts/daily_signal.py --lookback 30 --dry-run --out outputs/tmp/test_pack.md

# Override daily distro (admin only)
DAILY_DISTRO=config/email-distro-daily.json ./run-agent.sh daily
```

Exit codes:
- `0` — success (digest sent)
- `1` — pipeline failed (Phase A error, Claude subprocess failure, or HTML format error)

## Tunable parameters (already in DB)

All Stage 2 thresholds are tunable via the `tunable_parameters` table, seeded by `db_init.py`:

| Parameter | Default | Used by |
|---|---|---|
| `k_A1` | 0.60 | A1 range consumption |
| `k_A2` | 1.50 | A2 sigma exhaustion |
| `k_A3` | 0.40 | A3 IV expansion (MMD-deferred) |
| `k_A4_window` | 5 | A4 volume spike window (days) |
| `k_A4_mult` | 2.0 | A4 volume spike multiplier |
| `k_B1` | 1.0 | B1 earnings pass-through (MMD-deferred) |
| `k_B3` | 1.0 | B3 news absorption |
| `k_B4` | 1.5 | B4 sector rotation |
| `k_cluster_window` | 30 | Stage 4 lookback (days) |
| `threshold_open` | 1 | OWD "window open" max points |
| `threshold_narrowing` | 3 | OWD "window narrowing" max points |

## Verification

1. **Stock metrics smoke test** (yfinance + sector ETF lookups)
   ```bash
   python3 scripts/stock_metrics.py NVDA semiconductors 2025-08-01
   ```
   Expected: full dict with hist_range, vol, RSI, volume baseline, sector ETF metrics.

2. **Pipeline smoke test on a known trade**
   ```bash
   python3 scripts/pipeline.py SCORE_TRADE_ID 9597
   ```
   Expected: Stage 1 multiplier 1.0x, Stage 2 verdict, Stage 4 cluster count.

3. **Pipeline on top 5 latest buys**
   ```bash
   python3 scripts/pipeline.py SCORE_LATEST_BUYS 5
   ```
   Expected: each prints tier + multiplier + OWD verdict + cluster count.

4. **Cluster query**
   ```bash
   python3 scripts/pipeline.py CLUSTER NVDA
   ```
   Expected: dict with cluster_count, cross_party, politicians.

5. **Conceptual options helper**
   ```bash
   python3 scripts/options_concept.py
   ```
   Expected: 7 case examples printed (STRONG with catalyst, STRONG no catalyst, LEAPS, BASE, MODERATE, sell direction).

6. **Phase A driver dry-run on real data**
   ```bash
   ./run-agent.sh daily --lookback 200 --limit-trades 5 --dry-run
   ```
   Expected: Phase A scores ~600 trades, picks top 5 for LLM, writes a research pack with full per-trade context. No DB writes. Phase B does NOT run in dry-run.

7. **Direction-aware OWD check (regression test)**
   ```bash
   python3 -c "
   import sys; sys.path.insert(0, 'scripts')
   import db; from pipeline import score_trade
   conn = db.connect()
   row = dict(conn.execute('SELECT * FROM trades WHERE id = 9597').fetchone())
   r = score_trade(conn, row)
   assert 'A1' not in r['stage2']['checks_fired'], 'A1 should NOT fire when move is against the trade direction'
   print('OK: A1 correctly does not fire on losing trades')
   "
   ```
   Expected: passes. (Cisneros bought MSFT at $491, now $373 — down 24%; A1 must not fire because the move was against the buy.)

8. **Full live run** (after editing `config/email-distro-daily.json` to set production recipients)
   ```bash
   ./run-agent.sh daily
   ```
   Expected:
   - Daily digest email arrives
   - HTML report generated at `outputs/reports/daily_TIMESTAMP.html`
   - Logs at `outputs/logs/daily_TIMESTAMP.log`
   - DB rows added to `trade_diagnostics` and `recommendations`
   - If any STRONG, second email with `[STRONG SIGNAL]` subject also arrives

9. **Idempotency**
   ```bash
   ./run-agent.sh daily
   ./run-agent.sh daily   # second run, same day
   ```
   Expected: second run pulls 0 overnight trades because all are already in `recommendations`. Empty digest emitted (or could be suppressed via flag — implementation choice).

10. **Dispatch portability check**
    ```bash
    ls docs/plans/
    ```
    Expected: `phase-2-1-deep-dive.md`, `phase-2-2-data-maintenance.md`, `phase-2-3-daily-signal.md` all present.

## Edge cases handled

- **Empty overnight batch** → Phase A renders an empty research pack; runner still calls Claude which emits a "no trades to score" digest
- **yfinance returns None / NaN for a ticker** → Stage 2 checks return None, logged as `skipped_checks: ["A1: missing data"]`, trade still scored on remaining checks
- **Politician not in `politicians` table** (current state) → Stage 1 falls back to "off-committee" 0.3x unless trade is committee-aligned via the static sector mapping
- **More than 20 overnight trades** → top 20 by alignment × cluster get sent to LLM; rest get Stage 1+2+4 only and appear in the digest as MODERATE/SKIP
- **LLM returns malformed JSON block** → runner logs error, falls back to Stage 1+2+4 results, skips Stage 3 catalyst data
- **Same trade scored twice** → `upsert_trade_diagnostics` is idempotent on `trade_id`, last write wins
- **Trade has corrupted date** (year=2220, etc.) → pipeline doesn't crash; the `compute_actual_move` returns None and OWD checks skip
- **Move is AGAINST the trade direction** → A1, A2, B3 do NOT fire (regression test #7 above) — opportunity is actually MORE attractive with a better entry today
- **STRONG fires but daily distro is admin-only** → both emails (digest + alert) go to admin
- **Claude auth expired** → Phase B subprocess exits with auth markers; runner detects, exits 1, no email sent

## Known cascades from this work

- **Phase 2.3.5 = MMD integration.** Drops into `stock_metrics.py` (adds IV/earnings-move computation) and `pipeline.py` (uncomments A3 and B1 checks). `daily_signal.py` and the prompt need only minor tweaks. `options_concept.py` gets enriched with real strikes from MMD chains.
- **Phase 2.4 = Weekly Deep Research.** Reuses `pipeline.score_trade()` for re-scoring, `find_clustering()` for the weekly clustering view, and the same format_report → send_email path. Mostly aggregation + price-only retrospective on previously flagged trades.
- **Phase 3 = Feedback loop.** Reads `trade_diagnostics` historical rows, computes false-positive / false-negative rates per check, proposes parameter adjustments. The whole reason `trade_diagnostics` exists.
- **`--distro` flag in send_email.py** is now reusable for any future distro variant.
- **Direction-aware OWD bug fix** propagates: any future check that uses sigma_move or hist_range must also use favorable direction.

## Outstanding for Phase 2.3

- After the live email distro is edited, run `./run-agent.sh daily` end-to-end once to validate Claude subprocess + email delivery
- Validate the LLM correctly emits the fenced JSON block for round-tripping Stage 3 results (the prompt is explicit about this; will verify on first live run)
- Confirm STRONG-alert second email path works (rare event; will see in production)
