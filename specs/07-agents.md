# 07 — Agents & Notifications

## Agent 1 — Data Maintenance (a.k.a. "tracker")
**Schedule**: Daily, early morning (before Agent 2)
**Purpose**: Silent data pipeline
**Implementation**: Phase 2.2 — `scripts/data_maintenance.py` + `tracker` subcommand in `run-agent.sh`

**Work**:
1. Compute incremental `--since` date from `db.get_latest_disclosure_date()` minus 7-day safety buffer (catches retroactive filings + missed runs); fall back to 30 days if DB is empty.
2. Invoke `scripts/ingest.py --source house-efd --year YYYY --since YYYY-MM-DD --parse-pdfs` as a subprocess (default cap 200 PDFs/run).
3. Reconcile + dedupe via existing `ingest.reconcile()` + `db.upsert_trade()` CONFLICT clause; tag as `member_direct` / `spouse` / `dependent`.
4. Detect failures (non-zero exit, `[source] FAILED` markers, missing stats line) and dispatch admin-only alert via `send_email.py --to`.
5. January 1–15 rollover: also pull prior year to catch late-December filings.

**Output**: Silent on success. On failure, single HTML email to admin only (`anthonyjoonha@gmail.com` by default, overridable via `--admin`).

**Explicitly out of scope (do not reintroduce):**
- ~~Paper-trading mark-to-market~~ — paper trading was dropped during the brainstorm revision; not in this system at all.
- ~~Per-stock volatility metrics (`hist_range_45d`, `realized_vol_60d`, `sector_vol_60d`)~~ — these are only consumed by Agent 2's Stage 2 priced-in diagnostic. They will be computed alongside Stage 2 in Phase 2.3, not here. Putting them in Agent 1 creates an orphan dependency on infrastructure that doesn't yet consume them.
- ~~Capitol Trades scraping in Python~~ — current design defers to agent prompts using WebFetch. Phase 2.3+ may revisit.
- ~~Senate eFD~~ — deferred (Cloudflare session-cookie barrier).

## Agent 2 — Daily Signal (a.k.a. "daily")
**Schedule**: Weekday mornings, after Agent 1
**Purpose**: Score overnight disclosures, generate recommendations
**Implementation**: Phase 2.3 — `scripts/daily_signal.py` (Phase A driver) + `scripts/pipeline.py` (4-stage scoring engine) + `scripts/stock_metrics.py` (yfinance metrics) + `scripts/options_concept.py` (conceptual options) + `prompts/daily_signal.md` (Phase B narrative + Stage 3 catalyst search) + `daily` subcommand in `run-agent.sh`

**Work**:
1. Read overnight trades from DB via `db.get_overnight_trades()` (3-day rolling window, idempotent — already-scored trades skipped)
2. Phase A: Run Stages 1, 2 (Lite), and 4 deterministically per trade; persist to `trade_diagnostics` and the trades table's denormalized columns
3. Phase B: LLM does 5–10 web searches per surviving trade (80 max total) for forward catalysts (90-day window), then synthesizes a daily digest narrative
4. LLM emits a fenced JSON block at the end of the narrative with Stage 3 results + suggested final tiers; the runner parses it and writes back to the DB
5. Phase C: format HTML, dispatch daily digest via `config/email-distro-daily.json`
6. If any STRONG-tier play in the narrative subject line, fire a second `[STRONG SIGNAL]` email immediately

**Output**: Daily digest + potential instant STRONG alerts via Resend (admin-only by default; user edits `config/email-distro-daily.json` to add production daily distro).

**Stage 2 status**: Currently runs in **"Lite" mode** — 5 of 8 OWD checks. The two MMD-dependent checks (A3 IV expansion, B1 implied earnings move) will be added in Phase 2.3.5 when MMD is wired. Same thresholds; v1 is more conservative (under-flag rather than over-flag).

**Explicitly out of scope (do not reintroduce):**
- ~~Spouse trade media-coverage detection~~ — was item 4 in the original spec. Out of v1; revisit in Phase 3 if false-positive rates from spouse trades are too high.
- ~~Real options chains, real Greeks, specific strikes~~ — Phase 2.3.5 with MMD. v1 ships conceptual options only.
- ~~Paper-trading mark-to-market~~ — paper trading was dropped during the brainstorm revision; not in this system at all.

## Agent 3 — Weekly Deep Research
**Schedule**: Sunday night
**Purpose**: Deep analysis + performance review + parameter health

**Work**:
1. Review week's flagged trades from Agent 2
2. Pick top 5–10 for deep research (15–25 web searches per trade)
3. Pull full live options chain + Greeks via MMD for STRONG plays
4. Roll up paper-trading P&L: hit rates by signal tier / politician / committee / sector
5. Identify roster members with degrading performance → recommend demotion
6. **Run feedback loop** (see `specs/04-feedback.md`):
   - Compute FN/FP rates, per-check correlations, sensitivity analysis
   - Compute retroactive outcomes on filtered trades
   - Surface parameter adjustment proposals in "Parameter Health" section
7. Report spouse trade P&L separately

**Output**: Weekly email (~5–8K words) with Parameter Health section.

## Agent 4 — Politician Deep-Dive
**Schedule**: On-demand
**Invocation**: `./run-agent.sh deepdive "Josh Gottheimer"`
**Purpose**: Full profile on one politician

**Work**:
1. Pull all trades from DB
2. Compute committee-aligned hit rate (overall + recency-weighted)
3. Sector concentration, timing patterns vs legislative calendar
4. Spouse trade performance (separate)
5. 15–25 web searches on current committee work, statements, upcoming hearings

**Output**: Single-politician email to user only.

## Agent 5 — Backtest / Roster Generator
**Schedule**: Phase 0 + on-demand + quarterly recalibration
**Purpose**: Generate roster, re-validate, recalibrate parameters

**Work (roster mode)**:
1. Build candidate universe (~200–250 from 8 committees + leadership)
2. Seed 5yr historical trades from eFD + Capitol Trades + Finnhub
3. Filter to committee-aligned trades (time-varying membership)
4. Compute both metrics (overall + recency-weighted) vs SPY at 60-day
5. Apply pass bar (>5% vs SPY, >55% hit rate, both independently)
6. Floor: top 15 if <15 pass
7. Flag committee-transition members as probationary
8. Output roster + watchlist/fading-edge flags → write to `politicians` table

**Work (quarterly recalibration mode)**:
1. Replay trailing 6mo trades through OWD with grid search across all `k_XX`
2. Output "Quarterly Recalibration Report" with recommended parameter set
3. Human approval required before applying

**Output**: Roster report + roster written to DB. Recalibration report when in that mode.

## Notifications

| Channel | Trigger | Frequency |
|---|---|---|
| Daily digest | Agent 2 completes | 6 AM ET weekdays |
| `[STRONG SIGNAL]` alert | STRONG-tier play produced | A few times/month |
| Weekly deep report | Agent 3 completes | Sunday night |
| On-demand reports | Deep-dive, backtest, recalibration | As triggered |

**Delivery**: Resend API via `apesdegen.com`.
**Distribution**: Smaller than Trading project's 9 recipients. Exact list TBD.
