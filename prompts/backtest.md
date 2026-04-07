# Backtest Agent — Roster Generation

You are running the **Phase 0 Backtest Agent** for the CongressTrades project. Your job is to generate the initial tracked-politicians roster from data, not from intuition.

## Read First
- `specs/01-universe.md` — full backtest methodology, pass bar, recency weights
- `specs/06-data.md` — DB schema (politicians, trades, committee_mappings)
- `CLAUDE.md` — architecture overview

## Your Workflow

### 1. Verify the data layer is ready
Run these and confirm they return data:
```bash
python3 scripts/db.py params
python3 scripts/db.py mappings --ticker NVDA
sqlite3 data/congress.db "SELECT COUNT(*) FROM trades WHERE transaction_type='buy';"
```

If `trades` is empty or sparse, **run ingestion first**:
```bash
# Pull index of all 2025 filings + parse PTR PDFs in date window
python3 scripts/ingest.py --source house-efd --year 2025 --parse-pdfs --max-pdfs 200 --since 2025-01-01

# Repeat for prior years to seed 5-year backtest dataset
python3 scripts/ingest.py --source house-efd --year 2024 --parse-pdfs --max-pdfs 500
python3 scripts/ingest.py --source house-efd --year 2023 --parse-pdfs --max-pdfs 500
python3 scripts/ingest.py --source house-efd --year 2022 --parse-pdfs --max-pdfs 500
python3 scripts/ingest.py --source house-efd --year 2021 --parse-pdfs --max-pdfs 500
```

Each call is slow (downloads + parses PDFs). Increase `--max-pdfs` to cover more filings; expect a few hundred to a few thousand trades per year.

### 2. Augment with Capitol Trades (WebFetch)
For Senate coverage and ticker normalization, use WebFetch sparingly:
- `https://www.capitoltrades.com/trades?txDate=last-30-days`
- Limit to 1-2 fetches per agent run
- Parse the rendered table into `trades` rows via `db.upsert_trade`

### 3. Run the backtest engine
```bash
python3 scripts/backtest.py --lookback 5 --out outputs/reports/backtest_$(date +%Y-%m-%d).md
```

This will:
- Walk every distinct politician with at least 1 buy in `trades`
- For each, compute committee-aligned hit rate + excess return vs SPY at 60d
- Apply both metrics (overall + recency-weighted)
- Apply the pass bar (≥5% vs SPY AND ≥55% hit rate, both)
- Apply the floor (top 15 by hit rate if <15 pass)
- Write classified roster back to the `politicians` table

### 4. Review and report
Read the generated markdown report. Identify:
- How many politicians passed the bar?
- Did the floor have to be applied?
- Which politicians have the strongest committee-aligned edge?
- Any politicians flagged as "skipped" due to insufficient price data?

Compose an email-ready summary with:
- Headline: roster size, pass count, floor status
- Top 10 politicians by recency-weighted excess return
- Watchlist tier (fading edge)
- Skipped politicians and why
- Next steps: areas needing more historical data, Senate coverage gaps

### 5. Output
Write your full findings to:
```
outputs/tmp/backtest_<TIMESTAMP>_research.md
```

The runner script will format and email it.

## Constraints
- **Free sources only** — House eFD, Capitol Trades public pages, yfinance. No paid APIs.
- **No paper trading** — that's a downstream feature, not Phase 0.
- **Be honest about data quality** — if a politician's stats are based on N=2 trades, say so. Don't pretend statistical significance you don't have.
- **The pass bar is ambitious** — most politicians will fail it. That's the point.
