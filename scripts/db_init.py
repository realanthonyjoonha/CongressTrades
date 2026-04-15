#!/usr/bin/env python3
"""
db_init.py — Bootstrap SQLite schema for CongressTrades.

Idempotent: safe to re-run. Creates all tables defined in specs/06-data.md and
seeds tunable_parameters and committee_mappings from config files.

Usage:
    python3 scripts/db_init.py             # init at data/congress.db
    python3 scripts/db_init.py --reset     # drop all tables first
    python3 scripts/db_init.py --path /tmp/test.db
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "data" / "congress.db"
CONFIG_DIR = BASE_DIR / "config"


SCHEMA = """
CREATE TABLE IF NOT EXISTS politicians (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    chamber TEXT NOT NULL,                  -- 'house' | 'senate'
    party TEXT,                             -- 'D' | 'R' | 'I'
    roster_tier TEXT,                       -- 'core' | 'watchlist' | 'probationary' | 'candidate'
    committee_transition INTEGER DEFAULT 0, -- 0/1 bool
    committee_history TEXT,                 -- JSON [{committee, start_date, end_date}]
    leadership_history TEXT,                -- JSON [{position, scope, start_date, end_date}]
    last_updated TEXT,
    UNIQUE(name, chamber)
);

CREATE INDEX IF NOT EXISTS idx_politicians_tier ON politicians(roster_tier);
CREATE INDEX IF NOT EXISTS idx_politicians_chamber ON politicians(chamber);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    politician_id INTEGER REFERENCES politicians(id),
    politician_name TEXT,                   -- denormalized for fast queries
    trade_date TEXT NOT NULL,
    disclosure_date TEXT,
    ticker TEXT NOT NULL,
    sector TEXT,
    asset_type TEXT,                        -- 'stock' | 'option' | 'etf' | 'bond' | 'other'
    transaction_type TEXT,                  -- 'buy' | 'sell' | 'exchange'
    amount_range TEXT,                      -- "$1K-$15K" etc
    amount_min REAL,                        -- parsed lower bound
    amount_max REAL,                        -- parsed upper bound
    trader_tag TEXT,                        -- 'member_direct' | 'spouse' | 'dependent'

    -- Pipeline output
    alignment_multiplier REAL,
    owd_score_a INTEGER,
    owd_score_b INTEGER,
    owd_total INTEGER,
    owd_verdict TEXT,                       -- 'open' | 'narrowing' | 'closed'
    forward_catalyst TEXT,
    clustering_count INTEGER,
    cross_party_cluster INTEGER DEFAULT 0,
    final_signal_tier TEXT,                 -- 'STRONG' | 'BASE' | 'MODERATE' | 'SKIP'
    skip_reason TEXT,

    -- Provenance
    source TEXT,                            -- comma-separated list of sources
    source_id TEXT,                         -- ID from primary source for dedupe
    first_seen TEXT,
    last_updated TEXT,
    UNIQUE(politician_name, ticker, trade_date, transaction_type, amount_range)
);

CREATE INDEX IF NOT EXISTS idx_trades_politician ON trades(politician_id);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_disclosure_date ON trades(disclosure_date);
CREATE INDEX IF NOT EXISTS idx_trades_trade_date ON trades(trade_date);
CREATE INDEX IF NOT EXISTS idx_trades_signal_tier ON trades(final_signal_tier);

CREATE TABLE IF NOT EXISTS committee_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector TEXT,                            -- NULL for ticker-specific overrides
    committee TEXT NOT NULL,
    multi_jurisdiction INTEGER DEFAULT 0,
    ticker TEXT,                            -- NULL for sector-level
    rationale TEXT,
    last_updated TEXT
);

CREATE INDEX IF NOT EXISTS idx_mappings_sector ON committee_mappings(sector);
CREATE INDEX IF NOT EXISTS idx_mappings_ticker ON committee_mappings(ticker);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER REFERENCES trades(id),
    signal_tier TEXT,                       -- 'STRONG' | 'BASE' | 'MODERATE'
    option_type TEXT,                       -- 'call' | 'put' | 'spread' | 'conceptual'
    strike REAL,
    expiry TEXT,
    dte INTEGER,
    delta REAL, gamma REAL, theta REAL, vega REAL, iv REAL,
    bid REAL, ask REAL, mid REAL,
    entry_timestamp TEXT,
    thesis TEXT,
    bear_case TEXT,
    snapshot_caveat TEXT
);

CREATE INDEX IF NOT EXISTS idx_recs_trade ON recommendations(trade_id);
CREATE INDEX IF NOT EXISTS idx_recs_tier ON recommendations(signal_tier);

CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommendation_id INTEGER REFERENCES recommendations(id),
    entry_price REAL,
    current_price REAL,
    underlying_price REAL,
    pnl_30d REAL,
    pnl_60d REAL,
    pnl_90d REAL,
    status TEXT,                            -- 'open' | 'closed_expiry' | 'closed_bearcase'
    opened_date TEXT,
    closed_date TEXT,
    closed_pnl REAL
);

CREATE INDEX IF NOT EXISTS idx_paper_status ON paper_positions(status);

CREATE TABLE IF NOT EXISTS trade_diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER REFERENCES trades(id),
    -- Raw Dimension A values
    hist_range_45d REAL,
    realized_vol_60d REAL,
    sigma_move_val REAL,
    actual_price_move REAL,
    rsi REAL,
    iv_percentile REAL,
    iv_expansion REAL,
    volume_spike_detected INTEGER DEFAULT 0,
    -- Raw Dimension B values
    earnings_occurred INTEGER DEFAULT 0,
    actual_earnings_move REAL,
    implied_earnings_move REAL,
    legislative_event_occurred INTEGER DEFAULT 0,
    legislative_event_detail TEXT,
    news_catalyst_fired INTEGER DEFAULT 0,
    news_day_move REAL,
    sector_etf_move REAL,
    sector_vol_60d REAL,
    -- Computed thresholds
    threshold_a1 REAL, threshold_a2 REAL, threshold_a3 REAL,
    threshold_b1 REAL, threshold_b3 REAL, threshold_b4 REAL,
    -- Results
    checks_fired TEXT,                      -- JSON array
    verdict TEXT,
    -- Outcome
    outcome_type TEXT,                      -- 'actual' | 'retroactive'
    outcome_pnl_30d REAL,
    outcome_pnl_60d REAL,
    outcome_pnl_90d REAL,
    evaluated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_diag_trade ON trade_diagnostics(trade_id);
CREATE INDEX IF NOT EXISTS idx_diag_verdict ON trade_diagnostics(verdict);

CREATE TABLE IF NOT EXISTS parameter_changelog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    constant_name TEXT NOT NULL,
    old_value REAL,
    new_value REAL,
    rationale TEXT,
    approval_status TEXT,                   -- 'approved' | 'rejected' | 'pending'
    fn_rate_at_change REAL,
    fp_rate_at_change REAL
);

CREATE INDEX IF NOT EXISTS idx_changelog_const ON parameter_changelog(constant_name);

CREATE TABLE IF NOT EXISTS tunable_parameters (
    constant_name TEXT PRIMARY KEY,
    value REAL NOT NULL,
    description TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS sources_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,                   -- 'house_efd' | 'capitol_trades' | 'finnhub'
    fetched_at TEXT NOT NULL,
    request_url TEXT,
    raw_response TEXT,
    parsed_count INTEGER
);

CREATE INDEX IF NOT EXISTS idx_raw_source ON sources_raw(source);
CREATE INDEX IF NOT EXISTS idx_raw_fetched ON sources_raw(fetched_at);

-- Per-stock metric cache (Stage 2 input data)
CREATE TABLE IF NOT EXISTS stock_metrics (
    ticker TEXT PRIMARY KEY,
    hist_range_45d REAL,
    realized_vol_60d REAL,
    sector_vol_60d REAL,
    sector_etf TEXT,
    iv_range_12m REAL,
    iv_percentile_current REAL,
    implied_earnings_move REAL,
    next_earnings_date TEXT,
    last_price REAL,
    last_updated TEXT
);

-- Tracker run audit log (Phase 2.4)
-- One row per tracker invocation, even silent (0 new filings) runs.
-- The Daily Signal agent queries this table for "what the tracker found
-- between yesterday's daily and today's" as a research-pack memory section.
CREATE TABLE IF NOT EXISTS tracker_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp TEXT NOT NULL,           -- ISO datetime when run started
    completed_at TEXT,                     -- ISO datetime when run finished
    source TEXT,                           -- 'house-efd' | 'finnhub'
    year INTEGER,
    since_date TEXT,                       -- --since parameter used
    trades_persisted INTEGER,              -- from ingest stats
    placeholders_skipped INTEGER,
    new_trade_ids TEXT,                    -- JSON array of trade IDs added
    new_politician_names TEXT,             -- JSON array of unique politicians touched
    email_sent INTEGER DEFAULT 0,          -- 1 if Phase B composed + sent an email
    email_subject TEXT,
    phase_b_searches INTEGER,              -- how many web searches Sonnet made
    exit_code INTEGER,                     -- 0 = success
    failure_reason TEXT                    -- NULL on success
);

CREATE INDEX IF NOT EXISTS idx_tracker_runs_ts ON tracker_runs(run_timestamp);
CREATE INDEX IF NOT EXISTS idx_tracker_runs_email ON tracker_runs(email_sent);
"""

TABLES_TO_DROP = [
    "tracker_runs",
    "stock_metrics", "sources_raw", "tunable_parameters", "parameter_changelog",
    "trade_diagnostics", "paper_positions", "recommendations",
    "committee_mappings", "trades", "politicians",
]


DEFAULT_TUNABLE_PARAMS = [
    ("k_A1", 0.60, "Range consumption fraction"),
    ("k_A2", 1.50, "Sigma exhaustion multiplier"),
    ("k_A3", 0.40, "IV range fraction"),
    ("k_A4_window", 5, "Volume check window (days)"),
    ("k_A4_mult", 2.0, "Volume spike multiplier"),
    ("k_B1", 1.0, "Implied earnings move multiplier"),
    ("k_B3", 1.0, "News absorption sigma multiplier"),
    ("k_B4", 1.5, "Sector rotation sigma multiplier"),
    ("k_cluster_window", 30, "Clustering lookback (days)"),
    ("threshold_open", 1, "Max pts for window open"),
    ("threshold_narrowing", 3, "Max pts for window narrowing"),
    ("k_fn_threshold", 0.30, "False negative rate alert trigger"),
    ("k_fp_threshold", 0.40, "False positive rate alert trigger"),
]


def init_db(db_path: Path, reset: bool = False) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    if reset:
        for tbl in TABLES_TO_DROP:
            cur.execute(f"DROP TABLE IF EXISTS {tbl}")
        conn.commit()

    cur.executescript(SCHEMA)
    conn.commit()
    return conn


def seed_tunable_parameters(conn: sqlite3.Connection):
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    for name, value, desc in DEFAULT_TUNABLE_PARAMS:
        cur.execute(
            """INSERT INTO tunable_parameters (constant_name, value, description, last_updated)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(constant_name) DO NOTHING""",
            (name, value, desc, now),
        )
    conn.commit()


def seed_committee_mappings(conn: sqlite3.Connection):
    """Load sector + override mappings from config/committees.json if present."""
    cfg_path = CONFIG_DIR / "committees.json"
    if not cfg_path.exists():
        print(f"  [skip] config/committees.json not found yet, leaving committee_mappings empty")
        return

    with open(cfg_path) as f:
        cfg = json.load(f)

    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    inserted = 0

    # Sector-level mappings
    for sector, committees in cfg.get("sector_to_committee", {}).items():
        for committee in committees:
            cur.execute(
                """INSERT INTO committee_mappings (sector, committee, multi_jurisdiction, ticker, rationale, last_updated)
                   VALUES (?, ?, 0, NULL, ?, ?)""",
                (sector, committee, f"sector default: {sector} → {committee}", now),
            )
            inserted += 1

    # Mega-cap override list (multi-jurisdiction)
    for ticker_entry in cfg.get("mega_cap_override", []):
        ticker = ticker_entry["ticker"] if isinstance(ticker_entry, dict) else ticker_entry
        committees = ticker_entry.get("committees", []) if isinstance(ticker_entry, dict) else []
        rationale = ticker_entry.get("rationale", "mega-cap override") if isinstance(ticker_entry, dict) else "mega-cap override"
        if not committees:
            committees = ["override"]
        for committee in committees:
            cur.execute(
                """INSERT INTO committee_mappings (sector, committee, multi_jurisdiction, ticker, rationale, last_updated)
                   VALUES (NULL, ?, 1, ?, ?, ?)""",
                (committee, ticker, rationale, now),
            )
            inserted += 1

    conn.commit()
    print(f"  [seed] committee_mappings: {inserted} rows inserted")


def main():
    parser = argparse.ArgumentParser(description="Bootstrap CongressTrades SQLite DB")
    parser.add_argument("--path", default=str(DEFAULT_DB), help="DB path")
    parser.add_argument("--reset", action="store_true", help="Drop all tables first")
    args = parser.parse_args()

    db_path = Path(args.path)
    print(f"Initializing DB at {db_path}")
    if args.reset:
        print("  [reset] dropping all tables")

    conn = init_db(db_path, reset=args.reset)
    seed_tunable_parameters(conn)
    print(f"  [seed] tunable_parameters: {len(DEFAULT_TUNABLE_PARAMS)} defaults")
    seed_committee_mappings(conn)

    # Verify
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cur.fetchall()]
    print(f"\nTables created ({len(tables)}):")
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        count = cur.fetchone()[0]
        print(f"  {t}: {count} rows")

    conn.close()
    print(f"\nDone. DB ready at {db_path}")


if __name__ == "__main__":
    main()
