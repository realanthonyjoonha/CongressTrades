# 06 — Data Infrastructure

## Storage: SQLite

Source of truth: `data/congress.db` — single file, committed to git.

## Schema

```sql
-- Core roster
politicians (
  id, name, chamber, party,
  roster_tier TEXT,           -- 'core' | 'watchlist' | 'probationary'
  committee_transition BOOL,  -- changed committees in last 18mo
  -- historical committee assignments stored as JSON array of {committee, start_date, end_date}
  committee_history TEXT,
  -- leadership positions as JSON array of {position, scope, start_date, end_date}
  leadership_history TEXT
)

-- Every disclosed trade
trades (
  id, politician_id, trade_date, disclosure_date,
  ticker, sector, asset_type, transaction_type,  -- buy/sell
  amount_range TEXT,        -- "$1K-$15K" etc (STOCK Act ranges)
  trader_tag TEXT,          -- 'member_direct' | 'spouse' | 'dependent'
  -- Pipeline output columns:
  alignment_multiplier REAL,
  owd_score_a INT, owd_score_b INT, owd_total INT,
  owd_verdict TEXT,         -- 'open' | 'narrowing' | 'closed'
  forward_catalyst TEXT,
  clustering_count INT, cross_party_cluster BOOL,
  final_signal_tier TEXT,   -- 'STRONG' | 'BASE' | 'MODERATE' | 'SKIP'
  skip_reason TEXT
)

-- Sector → committee jurisdiction
committee_mappings (
  id, sector, committee, multi_jurisdiction BOOL,
  ticker TEXT,              -- NULL for sector-level, specific for override list
  rationale TEXT
)

-- Options plays
recommendations (
  id, trade_id,
  signal_tier, option_type, strike, expiry, dte,
  delta, gamma, theta, vega, iv,
  bid, ask, mid,
  entry_timestamp, thesis TEXT, bear_case TEXT,
  snapshot_caveat TEXT
)

-- Paper portfolio
paper_positions (
  id, recommendation_id,
  entry_price, current_price, underlying_price,
  pnl_30d, pnl_60d, pnl_90d,
  status TEXT,              -- 'open' | 'closed_expiry' | 'closed_bearcase'
  closed_date, closed_pnl
)

-- Feedback loop
trade_diagnostics (
  id, trade_id,
  -- Raw Dimension A values
  hist_range_45d, realized_vol_60d, sigma_move_val,
  actual_price_move, rsi, iv_percentile, iv_expansion,
  volume_spike_detected BOOL,
  -- Raw Dimension B values
  earnings_occurred BOOL, actual_earnings_move, implied_earnings_move,
  legislative_event_occurred BOOL, legislative_event_detail TEXT,
  news_catalyst_fired BOOL, news_day_move,
  sector_etf_move, sector_vol_60d,
  -- Computed thresholds at eval time
  threshold_a1, threshold_a2, threshold_a3, threshold_b1, threshold_b3, threshold_b4,
  -- Results
  checks_fired TEXT,        -- JSON array of check names that fired
  verdict TEXT,
  -- Outcome (actual if recommended, retroactive if filtered)
  outcome_type TEXT,        -- 'actual' | 'retroactive'
  outcome_pnl_30d, outcome_pnl_60d, outcome_pnl_90d
)

parameter_changelog (
  id, timestamp, constant_name,
  old_value REAL, new_value REAL,
  rationale TEXT, approval_status TEXT,  -- 'approved' | 'rejected' | 'pending'
  fn_rate_at_change REAL, fp_rate_at_change REAL
)

tunable_parameters (
  constant_name TEXT PRIMARY KEY,
  value REAL,
  last_updated TEXT
)

-- Raw ingestion for audit
sources_raw (
  id, source TEXT, fetched_at, raw_response TEXT
)
```

## JSON Exports
`data/exports/*.json` — derived views agents can read without SQL.

## Data Sources

### Trade Data
| Source | Purpose | Method |
|---|---|---|
| House eFD XML archive | Primary House source, 5yr history | Python XML parse |
| Capitol Trades | Normalize names, fill Senate gaps | WebFetch, date-filtered, 1–2 fetches/run |
| Finnhub API | Tertiary reconciliation | API calls |
| Senate eFD | Deferred v1 — Cloudflare agreement page too fragile | — |

### Price & Volatility Data
| Source | Provides | Cost |
|---|---|---|
| yfinance | Historical OHLC, realized vol, sector ETF vol, RSI | Free, no API key |
| Massive Market Data (MMD) | Options chains, Greeks, IV percentile, IV history, implied earnings moves | Existing subscription |

### Per-Stock Metrics (cached daily by Agent 1)
| Metric | Source |
|---|---|
| `hist_range_45d` | yfinance |
| `realized_vol_60d` | yfinance |
| `sector_vol_60d` | yfinance (sector ETF) |
| `iv_range_12m` | MMD |
| `implied_earnings_move` | MMD (pre-earnings straddle) |
