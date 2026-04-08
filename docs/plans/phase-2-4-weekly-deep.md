# Phase 2.4 — Weekly Deep Research Agent (Agent 5, "weekly")

## Context

Phases 2.1–2.3 shipped the four daily-cadence agents: Deep-Dive (on-demand), Data Maintenance (silent pipeline), and Daily Signal (the core deliverable). Phase 2.4 ships the **last** agent in the system: the Sunday-night Weekly Deep Research agent.

Where Daily Signal is fast and tactical (scores overnight disclosures in minutes, 5–10 searches per trade, daily digest email), Weekly Deep is slow and thorough: it aggregates the week's flagged trades, runs a price-only retrospective vs SPY, re-scores everything to catch state changes, and does 15 searches per deep-dive trade (150 max budget) for a multi-thousand-word weekly report.

This phase also fixes a gap I discovered in Phase 2.3: the `daily` runner was not parsing the LLM's fenced JSON block and writing Stage 3 results + recommendations back to the DB. Without that writeback, Phase 2.4's retrospective would have no `forward_catalyst` data or `recommendations` rows to work with. The writeback is included here as a "Phase 2.3 completion" item.

## Scope

### IN scope

1. **Phase 2.3 writeback (completion of Phase 2.3):**
   - `daily_signal.extract_json_block()` — parse the fenced `\`\`\`json ... \`\`\`` block at the end of an LLM narrative
   - `daily_signal.apply_llm_writeback()` — for each trade in the JSON block, look up Stage 1/2/4 from the DB, call `pipeline.assemble_final_tier()` with the LLM's forward_catalyst to compute the final tier, update `trades.forward_catalyst` + `trades.final_signal_tier`, insert into `recommendations` for STRONG/BASE tiers
   - New CLI subcommand: `python3 scripts/daily_signal.py --writeback NARRATIVE_PATH`
   - Runner `daily` subcommand gets a new "Phase C1 — writeback" step before formatting HTML
   - Runner ALSO strips the fenced JSON block from the HTML before emailing (it's internal machinery, readers shouldn't see it)

2. **Weekly Deep Research agent (Phase 2.4 proper):**
   - `scripts/weekly_deep.py` Phase A driver — fetches weekly flagged trades, computes retrospective, rescores, aggregates, renders pack
   - `prompts/weekly_deep.md` Phase B prompt — 9-section weekly report with required SUBJECT line + required final JSON block
   - `run-agent.sh weekly` subcommand — 3-phase pattern mirroring `daily`
   - `db.get_weekly_flagged_trades()` helper
   - `config/email-distro-daily.json` is reused for weekly (override via `WEEKLY_DISTRO` env var)

3. **Price-only retrospective:**
   - Stock-level entry price (via backtest's persistent cache) vs current close
   - vs SPY benchmark (same window)
   - Days held, excess return, beat-SPY boolean
   - No options math, no paper P&L, no position sizing

4. **Weekly rescore:**
   - Re-run `pipeline.score_trade` on every flagged trade to catch state changes since Daily Signal
   - Diffs (trades whose tier flipped) surface in the narrative as "rescore alerts"
   - Reasons: clustering grew, OWD window narrowed, new trades in the same ticker changed the cross-party count

5. **Weekly aggregations:**
   - Tier counts (STRONG/BASE/MODERATE/SKIP)
   - By politician (top 15 with tier breakdown)
   - By sector (top 10)
   - Cluster hot list (tickers with 3+ politicians this week)
   - Top 5 winners / losers by excess vs SPY
   - Benchmark hit rate (% beat SPY)

6. **LLM selection:**
   - Top 10 by tier priority (STRONG > BASE > MODERATE), then clustering count, then excess return
   - Dropped from selection: trades whose rescore flipped to SKIP
   - 15 searches/trade × 10 trades = 150 max web search budget

7. **LLM narrative:**
   - 9-section report: Executive Summary, Tier Breakdown, Retrospective, Rescore Alerts, Cluster Hot List, Deep-Dive Analysis (400-600 words each), Roster Health (observational only), Forward Watchlist, Methodology + JSON block
   - Length target: 4,000–8,000 words

### OUT of scope

- **Paper trading P&L rollup** — dropped permanently; not in this system
- **Automated roster demotion** — surface observations only; user decides
- **Feedback loop / parameter recalibration** — Phase 3 work, separate agent
- **Real options notional via MMD for STRONG plays** — Phase 2.3.5 dependency
- **Senate trade coverage** — still blocked at ingestion
- **Cron / launchd wiring** — separate task
- **Spouse trade P&L** — inherited from paper-trading drop
- **Parameter Health section in the weekly report** — requires the feedback loop (Phase 3); out of v1
- **Historical weekly replays** — would be valuable for calibration but separate scope

## Architecture

```
run-agent.sh weekly
    │
    ▼
Phase A: scripts/weekly_deep.py
    ├─ db.get_weekly_flagged_trades(lookback=7)
    ├─ FOR EACH trade:
    │     ├─ compute_retrospective()          [entry vs current vs SPY]
    │     └─ rescore_trade()                  [pipeline.score_trade for state changes]
    ├─ aggregate_week()                       [counts + rollups + cluster hot list]
    ├─ select_for_llm(top 10 by priority)
    └─ render_weekly_pack() → outputs/tmp/weekly_*_pack.md
    │
    ▼
Phase B: prompts/weekly_deep.md + claude --print
    ├─ Read research pack
    ├─ FOR EACH selected trade:
    │     ├─ 15 web searches across 4 buckets (forward catalysts, analyst,
    │     │   structural, thesis validation)
    │     └─ Synthesize deep-dive subsection
    ├─ Aggregate into 9-section weekly narrative
    └─ Emit fenced JSON block with per-trade findings
    │
    ▼
Phase C: run-agent.sh (bash)
    ├─ grep SUBJECT line from narrative
    ├─ Strip SUBJECT + fenced JSON block (internal)
    ├─ format_report.py → HTML
    └─ send_email.py --distro config/email-distro-daily.json
```

## Files shipped

| File | Status | Notes |
|---|---|---|
| `scripts/daily_signal.py` | MODIFIED (+~200 lines) | New `extract_json_block()` and `apply_llm_writeback()` functions. New `--writeback NARRATIVE_PATH` CLI subcommand. The writeback is invoked by `run-agent.sh daily` as "Phase C1" before HTML formatting. |
| `scripts/weekly_deep.py` | NEW (~520 lines) | Phase A driver. fetch_weekly_flagged, compute_retrospective, rescore_trade, aggregate_week, select_for_llm, render_weekly_pack. |
| `prompts/weekly_deep.md` | NEW (~220 lines) | Phase B prompt. 9-section report structure, 150-search budget, required SUBJECT + final JSON block. |
| `scripts/db.py` | MODIFIED (+50 lines) | New `get_weekly_flagged_trades(lookback=7, tiers=[...])` helper. Sorts by tier priority then clustering count then recency. |
| `run-agent.sh` | MODIFIED (+~120 lines) | New `weekly` subcommand (3-phase pattern). `daily` subcommand gets the new Phase C1 writeback step + strips JSON block from HTML output. Help text updated: all 5 agents now active. |
| `specs/07-agents.md` | MODIFIED | Agent 5 (Weekly Deep Research) section rewritten to reflect actual scope. Dropped paper-trading + automated demotion + feedback loop items with explicit "do not reintroduce" callouts. Renamed from "Agent 3" to "Agent 5" to match CLAUDE.md's numbering (the brainstorm-era "Agent 3 Weekly" was misnumbered in this file). |
| `docs/plans/phase-2-4-weekly-deep.md` | NEW | This document. |

## Verification

1. **Writeback JSON parser smoke test**
   ```bash
   python3 -c "
   import sys; sys.path.insert(0, 'scripts')
   from daily_signal import extract_json_block
   sample = '''some prose
   \`\`\`json
   {\"trades\": [{\"trade_id\": 9597}]}
   \`\`\`'''
   r = extract_json_block(sample)
   assert r and r['trades'][0]['trade_id'] == 9597
   print('PASS')
   "
   ```

2. **Writeback end-to-end test**
   - Run Phase A on 2 known trades to populate trades.alignment_multiplier etc.
   - Synthesize a fake LLM narrative with a fenced JSON block
   - Run `python3 scripts/daily_signal.py --writeback NARRATIVE_PATH`
   - Verify `trades.forward_catalyst` + `trades.final_signal_tier` updated
   - Verify `recommendations` row inserted for STRONG/BASE tiers
   - Verify clean cleanup after test

3. **Weekly Phase A on real data**
   ```bash
   python3 scripts/weekly_deep.py --lookback 200 --dry-run --out outputs/tmp/weekly_smoke.md
   ```
   Expected: pack with weekly summary + retrospective + selected deep-dives. Empty if no `final_signal_tier` has been populated yet (run daily --writeback first to populate).

4. **DB helper smoke test**
   ```bash
   python3 -c "
   import sys; sys.path.insert(0, 'scripts')
   import db
   conn = db.connect()
   rows = db.get_weekly_flagged_trades(conn, lookback_days=200)
   print(f'flagged trades: {len(rows)}')
   for r in rows[:3]:
       print(f'  {dict(r)[\"politician_name\"]}: {dict(r)[\"final_signal_tier\"]}')
   "
   ```

5. **Bash syntax check**
   ```bash
   bash -n run-agent.sh && echo OK
   ```

6. **Runner help**
   ```bash
   ./run-agent.sh help
   ```
   Expected: all 5 agents listed ("All agents active").

7. **Full live E2E** (after production email distro is set)
   ```bash
   ./run-agent.sh weekly
   ```
   Expected:
   - Weekly email arrives with retrospective + deep-dives
   - HTML report at `outputs/reports/weekly_TIMESTAMP.html`
   - Logs at `outputs/logs/weekly_TIMESTAMP.log`
   - JSON block stripped from the emailed HTML (internal machinery)

8. **Dispatch portability check**
   ```bash
   ls docs/plans/
   ```
   Expected: `phase-2-1-deep-dive.md`, `phase-2-2-data-maintenance.md`, `phase-2-3-daily-signal.md`, `phase-2-4-weekly-deep.md`.

## Edge cases handled

- **Empty flagged list** (no trades passed through Daily Signal this week) → empty research pack, LLM writes short "no flagged trades" report, email still sent
- **Retrospective data missing** (yfinance returned None for a ticker) → trade appears in the pack with `(data unavailable)` and is excluded from winners/losers tables
- **Rescore error** for one trade → logged with `error` key, trade still appears in other sections
- **Rescore flips to SKIP** → trade excluded from LLM selection but still counted in aggregates
- **150-search budget exhausted** mid-way through deep-dives → prompt explicitly tells the LLM to handle this gracefully
- **Writeback: trade_id not in DB** → logged as error in writeback summary, other trades still processed
- **Writeback: malformed JSON block** → returns None, runner logs warning, email still sent
- **Writeback: past catalyst evidence surfaced** (LLM found a fired catalyst) → runner honors the LLM's "SKIP" suggestion and downgrades the trade

## Known cascades from this work

After Phase 2.4:
- **All 5 agents are active.** Phase 2 complete.
- **The `extract_json_block` utility** is reusable — Phase 3 feedback loop will use the same pattern to parse structured output from other LLM runs.
- **The `compute_retrospective` helper** is reusable — Phase 3 feedback loop will call it on historical trades to backfill outcome data into `trade_diagnostics`.
- **The `rescore_trade` helper** can power a "what would happen if we re-scored all recent trades with new parameters" tool once the feedback loop proposes parameter changes.
- **The weekly JSON block is informational only in v1** — Phase 3 feedback loop will add a writeback step that consumes it (similar to daily's writeback).
- **`docs/plans/` directory** now has one file per phase; Dispatch-portable development is fully established.

## Outstanding for Phase 2.4

- After the full agent roster is live, run `./run-agent.sh daily` end-to-end once to validate the writeback actually persists forward_catalyst + recommendations
- Run `./run-agent.sh weekly` end-to-end once to validate the deep-research flow
- Consider a follow-up to add a writeback step to the weekly subcommand too (for Phase 3 feedback loop)
- Consider adding a "no-op" mode to weekly so it can be safely scheduled without sending email when the research pack is empty
