#!/usr/bin/env python3
"""
db.py — SQLite helper module for CongressTrades.

Provides a thin functional API over data/congress.db. All scripts and prompts
should go through this module rather than touching sqlite3 directly. Keeps the
schema centralized and makes future migrations safe.

Usage from other Python scripts:

    from db import connect, upsert_politician, upsert_trade, get_param

    conn = connect()
    pid = upsert_politician(conn, name="Nancy Pelosi", chamber="house", party="D")
    upsert_trade(conn, politician_name="Nancy Pelosi", ticker="NVDA", ...)
    k_a1 = get_param(conn, "k_A1")

CLI mode for ad-hoc queries:

    python3 scripts/db.py params
    python3 scripts/db.py politicians
    python3 scripts/db.py trades --ticker NVDA
    python3 scripts/db.py trades --politician "Nancy Pelosi" --limit 20
    python3 scripts/db.py mappings --ticker NVDA
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "congress.db"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    """Open a connection to the project DB. Returns Row-factory connection."""
    p = Path(path) if path else DB_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"DB not found at {p}. Run: python3 scripts/db_init.py"
        )
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso() -> str:
    return datetime.utcnow().isoformat()


# ---------------------------------------------------------------------------
# Politicians
# ---------------------------------------------------------------------------

def upsert_politician(
    conn: sqlite3.Connection,
    name: str,
    chamber: str,
    party: Optional[str] = None,
    roster_tier: str = "candidate",
    committee_history: Optional[List[Dict]] = None,
    leadership_history: Optional[List[Dict]] = None,
    committee_transition: bool = False,
) -> int:
    """Insert or update a politician. Returns the row id."""
    cur = conn.cursor()
    ts = now_iso()
    cur.execute(
        """
        INSERT INTO politicians
            (name, chamber, party, roster_tier, committee_transition,
             committee_history, leadership_history, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name, chamber) DO UPDATE SET
            party = COALESCE(excluded.party, politicians.party),
            roster_tier = excluded.roster_tier,
            committee_transition = excluded.committee_transition,
            committee_history = COALESCE(excluded.committee_history, politicians.committee_history),
            leadership_history = COALESCE(excluded.leadership_history, politicians.leadership_history),
            last_updated = excluded.last_updated
        """,
        (
            name,
            chamber,
            party,
            roster_tier,
            int(bool(committee_transition)),
            json.dumps(committee_history) if committee_history else None,
            json.dumps(leadership_history) if leadership_history else None,
            ts,
        ),
    )
    conn.commit()

    cur.execute(
        "SELECT id FROM politicians WHERE name = ? AND chamber = ?",
        (name, chamber),
    )
    row = cur.fetchone()
    return row["id"] if row else -1


def get_politician(conn: sqlite3.Connection, name: str, chamber: Optional[str] = None) -> Optional[sqlite3.Row]:
    cur = conn.cursor()
    if chamber:
        cur.execute("SELECT * FROM politicians WHERE name = ? AND chamber = ?", (name, chamber))
    else:
        cur.execute("SELECT * FROM politicians WHERE name = ?", (name,))
    return cur.fetchone()


def list_politicians(
    conn: sqlite3.Connection, tier: Optional[str] = None
) -> List[sqlite3.Row]:
    cur = conn.cursor()
    if tier:
        cur.execute("SELECT * FROM politicians WHERE roster_tier = ? ORDER BY name", (tier,))
    else:
        cur.execute("SELECT * FROM politicians ORDER BY name")
    return cur.fetchall()


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def upsert_trade(
    conn: sqlite3.Connection,
    politician_name: str,
    ticker: str,
    trade_date: str,
    transaction_type: str,
    amount_range: Optional[str] = None,
    disclosure_date: Optional[str] = None,
    sector: Optional[str] = None,
    asset_type: str = "stock",
    trader_tag: str = "member_direct",
    source: str = "unknown",
    source_id: Optional[str] = None,
    politician_id: Optional[int] = None,
) -> int:
    """
    Insert a trade or update its 'last_updated' / 'source' if it already exists
    (idempotent on (politician, ticker, trade_date, transaction_type, amount_range)).
    Returns the row id.
    """
    cur = conn.cursor()
    ts = now_iso()
    amount_min, amount_max = parse_amount_range(amount_range)

    cur.execute(
        """
        INSERT INTO trades
            (politician_id, politician_name, trade_date, disclosure_date, ticker, sector,
             asset_type, transaction_type, amount_range, amount_min, amount_max, trader_tag,
             source, source_id, first_seen, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(politician_name, ticker, trade_date, transaction_type, amount_range) DO UPDATE SET
            disclosure_date = COALESCE(excluded.disclosure_date, trades.disclosure_date),
            sector = COALESCE(excluded.sector, trades.sector),
            source = CASE
                WHEN trades.source LIKE '%' || excluded.source || '%' THEN trades.source
                ELSE trades.source || ',' || excluded.source
            END,
            last_updated = excluded.last_updated
        """,
        (
            politician_id,
            politician_name,
            trade_date,
            disclosure_date,
            ticker.upper() if ticker else None,
            sector,
            asset_type,
            transaction_type,
            amount_range,
            amount_min,
            amount_max,
            trader_tag,
            source,
            source_id,
            ts,
            ts,
        ),
    )
    conn.commit()

    cur.execute(
        """
        SELECT id FROM trades
        WHERE politician_name = ? AND ticker = ? AND trade_date = ?
              AND transaction_type = ? AND COALESCE(amount_range, '') = COALESCE(?, '')
        """,
        (politician_name, ticker.upper() if ticker else None, trade_date,
         transaction_type, amount_range),
    )
    row = cur.fetchone()
    return row["id"] if row else -1


def parse_amount_range(amount_range: Optional[str]) -> tuple:
    """
    Parse a STOCK Act amount range string into (min, max) numeric values.
    Examples:
        "$1,001 - $15,000"  -> (1001, 15000)
        "$15K - $50K"       -> (15000, 50000)
        "$1M - $5M"         -> (1000000, 5000000)
        None or unparseable -> (None, None)
    """
    if not amount_range:
        return (None, None)

    s = amount_range.replace("$", "").replace(",", "").strip()

    def to_number(token: str) -> Optional[float]:
        token = token.strip().upper().replace(" ", "")
        if not token:
            return None
        mult = 1.0
        if token.endswith("K"):
            mult = 1_000
            token = token[:-1]
        elif token.endswith("M"):
            mult = 1_000_000
            token = token[:-1]
        try:
            return float(token) * mult
        except ValueError:
            return None

    parts = s.split("-")
    if len(parts) != 2:
        n = to_number(s)
        return (n, n)
    return (to_number(parts[0]), to_number(parts[1]))


def query_trades(
    conn: sqlite3.Connection,
    ticker: Optional[str] = None,
    politician_name: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    transaction_type: Optional[str] = None,
    signal_tier: Optional[str] = None,
    limit: int = 100,
) -> List[sqlite3.Row]:
    """Generic trade query. All filters optional."""
    where = []
    params = []
    if ticker:
        where.append("ticker = ?")
        params.append(ticker.upper())
    if politician_name:
        where.append("politician_name = ?")
        params.append(politician_name)
    if since:
        where.append("trade_date >= ?")
        params.append(since)
    if until:
        where.append("trade_date <= ?")
        params.append(until)
    if transaction_type:
        where.append("transaction_type = ?")
        params.append(transaction_type)
    if signal_tier:
        where.append("final_signal_tier = ?")
        params.append(signal_tier)

    sql = "SELECT * FROM trades"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY trade_date DESC LIMIT ?"
    params.append(limit)

    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def trades_in_window(
    conn: sqlite3.Connection,
    ticker: str,
    days: int,
    end_date: Optional[str] = None,
) -> List[sqlite3.Row]:
    """Return trades for a ticker in the past N days from end_date (default today)."""
    cur = conn.cursor()
    if end_date is None:
        end_date = datetime.utcnow().strftime("%Y-%m-%d")
    cur.execute(
        """
        SELECT * FROM trades
        WHERE ticker = ?
          AND trade_date <= ?
          AND DATE(trade_date) >= DATE(?, ?)
        ORDER BY trade_date DESC
        """,
        (ticker.upper(), end_date, end_date, f"-{days} days"),
    )
    return cur.fetchall()


def get_latest_disclosure_date(
    conn: sqlite3.Connection,
    source: Optional[str] = None,
) -> Optional[str]:
    """
    Return the most recent disclosure_date in the trades table, optionally
    filtered by source. Used by the Data Maintenance agent (Agent 2) to compute
    the incremental --since window for daily ingestion.

    The `source` filter does a LIKE match because the trades table merges
    multiple sources into a single comma-separated source field during
    reconcile() — e.g. "house_efd,finnhub". Pass "house_efd" to filter to
    rows that include that source.

    Returns ISO date string "YYYY-MM-DD" or None if no trades match.
    """
    cur = conn.cursor()
    if source:
        cur.execute(
            "SELECT MAX(disclosure_date) FROM trades WHERE source LIKE ?",
            (f"%{source}%",),
        )
    else:
        cur.execute("SELECT MAX(disclosure_date) FROM trades")
    row = cur.fetchone()
    if row and row[0]:
        return row[0]
    return None


# ---------------------------------------------------------------------------
# Daily Signal helpers (Phase 2.3)
# ---------------------------------------------------------------------------

def get_weekly_flagged_trades(
    conn: sqlite3.Connection,
    lookback_days: int = 7,
    tiers: Optional[List[str]] = None,
) -> List[sqlite3.Row]:
    """
    Pull trades flagged by the Daily Signal agent in the trailing N days.
    Used by the Weekly Deep Research agent (Phase 2.4) to aggregate the
    week's actionable trades.

    A trade is considered "flagged" if it has a non-null final_signal_tier
    (which means Daily Signal Phase A has scored it). The lookback window
    matches on `last_updated` (set by update_trade_pipeline_columns),
    which is when Daily Signal last touched the row.

    Args:
        lookback_days: trailing window in days from today
        tiers: optional list of tier names to filter to, e.g.
               ['STRONG', 'BASE']. Defaults to STRONG + BASE + MODERATE.

    Returns: list of sqlite3.Row from trades table, sorted by
             tier priority (STRONG first) then recency.
    """
    if tiers is None:
        tiers = ["STRONG", "BASE", "MODERATE"]

    today = datetime.utcnow().strftime("%Y-%m-%d")
    placeholders = ", ".join(["?"] * len(tiers))
    sql = f"""
        SELECT t.* FROM trades t
        WHERE t.final_signal_tier IN ({placeholders})
          AND DATE(t.last_updated) >= DATE(?, ?)
        ORDER BY
          CASE t.final_signal_tier
            WHEN 'STRONG'   THEN 1
            WHEN 'BASE'     THEN 2
            WHEN 'MODERATE' THEN 3
            ELSE 4
          END,
          t.clustering_count DESC,
          t.last_updated DESC
    """
    params: List = list(tiers) + [today, f"-{lookback_days} days"]
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


def insert_tracker_run(
    conn: sqlite3.Connection,
    run_data: Dict,
) -> int:
    """
    Persist a tracker run's outcome to `tracker_runs`. Called once per
    tracker invocation (even silent runs with 0 new trades).

    Expected keys in run_data (all optional except run_timestamp):
        run_timestamp (str, required), completed_at, source, year,
        since_date, trades_persisted, placeholders_skipped,
        new_trade_ids (list[int]), new_politician_names (list[str]),
        email_sent (bool), email_subject, phase_b_searches, exit_code,
        failure_reason

    Returns the inserted row id.
    """
    schema_cols = [
        "run_timestamp", "completed_at", "source", "year", "since_date",
        "trades_persisted", "placeholders_skipped",
        "new_trade_ids", "new_politician_names",
        "email_sent", "email_subject", "phase_b_searches",
        "exit_code", "failure_reason",
    ]
    cols: List[str] = []
    placeholders: List[str] = []
    values: List = []
    for c in schema_cols:
        if c in run_data:
            v = run_data[c]
            if isinstance(v, (list, dict)):
                v = json.dumps(v)
            if isinstance(v, bool):
                v = 1 if v else 0
            cols.append(c)
            placeholders.append("?")
            values.append(v)

    if "run_timestamp" not in run_data:
        raise ValueError("insert_tracker_run requires run_timestamp")

    sql = (
        f"INSERT INTO tracker_runs ({', '.join(cols)}) "
        f"VALUES ({', '.join(placeholders)})"
    )
    cur = conn.cursor()
    cur.execute(sql, values)
    conn.commit()
    return cur.lastrowid or -1


def get_tracker_runs_since(
    conn: sqlite3.Connection,
    hours: int = 24,
    only_with_email: bool = False,
) -> List[sqlite3.Row]:
    """
    Read recent tracker_runs rows for the Daily Signal agent's "memory"
    section. By default returns runs from the trailing N hours. When
    `only_with_email=True`, filters to runs that actually composed and
    sent a tracker email (skipping silent 0-filing runs).
    """
    cur = conn.cursor()
    # SQLite DATE() arithmetic on datetime strings works; for hour-level
    # precision we use datetime() with a modifier.
    sql = """
        SELECT * FROM tracker_runs
        WHERE datetime(run_timestamp) >= datetime('now', ?)
    """
    params: List = [f"-{hours} hours"]
    if only_with_email:
        sql += " AND email_sent = 1"
    sql += " ORDER BY run_timestamp DESC"
    cur.execute(sql, params)
    return cur.fetchall()


def get_recommendations_since(
    conn: sqlite3.Connection,
    days: int = 7,
    tiers: Optional[List[str]] = None,
) -> List[sqlite3.Row]:
    """
    Read recommendations created in the trailing N days, joined with the
    trades table for context (ticker, politician, trade_date, amount_range).
    Used by the Daily Signal agent to surface "what we flagged recently"
    as context for today's run.

    Args:
        days: trailing window in days
        tiers: optional filter, e.g. ['STRONG', 'BASE'] for the
               "actionable trades only" view that the user asked for.

    Returns list of Row objects with all recommendations columns plus
    ticker, politician_name, trade_date, amount_range from trades.
    """
    cur = conn.cursor()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    # Parameter order follows the SQL placeholder order: date filter first,
    # then optional tier list. Getting this wrong silently returns zero rows.
    params: List = [today, f"-{days} days"]
    placeholders_clause = ""
    if tiers:
        placeholders = ", ".join(["?"] * len(tiers))
        placeholders_clause = f"AND r.signal_tier IN ({placeholders})"
        params.extend(list(tiers))
    sql = f"""
        SELECT
            r.id as recommendation_id,
            r.trade_id,
            r.signal_tier,
            r.option_type,
            r.strike,
            r.expiry,
            r.dte,
            r.delta,
            r.entry_timestamp,
            r.thesis,
            r.bear_case,
            t.ticker,
            t.politician_name,
            t.trade_date,
            t.disclosure_date,
            t.amount_range,
            t.sector,
            t.clustering_count
        FROM recommendations r
        JOIN trades t ON r.trade_id = t.id
        WHERE DATE(r.entry_timestamp) >= DATE(?, ?)
        {placeholders_clause}
        ORDER BY r.entry_timestamp DESC
    """
    cur.execute(sql, params)
    return cur.fetchall()


def get_overnight_trades(
    conn: sqlite3.Connection,
    lookback_days: int = 3,
    only_buys: bool = True,
    exclude_dependent: bool = True,
) -> List[sqlite3.Row]:
    """
    Pull trades disclosed in the trailing N days that have NOT yet been
    scored (i.e., not present in the recommendations table).

    Used by the Daily Signal agent to find "overnight disclosures" each
    morning. Lookback of 3 days catches Friday/Sat/Sun rollover when the
    agent runs Monday morning.

    The exclusion against `recommendations` makes the operation idempotent:
    re-running the same day pulls 0 trades since they're already scored.

    Args:
        lookback_days: trailing window in days from today
        only_buys: filter to transaction_type='buy' only
        exclude_dependent: filter out trader_tag='dependent' (per spec, those
                          are logged but not scored)

    Returns: list of sqlite3.Row from trades table.
    """
    cur = conn.cursor()
    today = datetime.utcnow().strftime("%Y-%m-%d")
    sql = """
        SELECT t.* FROM trades t
        WHERE t.disclosure_date IS NOT NULL
          AND DATE(t.disclosure_date) >= DATE(?, ?)
          AND DATE(t.disclosure_date) <= DATE(?)
          AND t.id NOT IN (SELECT trade_id FROM recommendations WHERE trade_id IS NOT NULL)
    """
    params: List = [today, f"-{lookback_days} days", today]
    if only_buys:
        sql += " AND t.transaction_type = 'buy'"
    if exclude_dependent:
        sql += " AND (t.trader_tag IS NULL OR t.trader_tag != 'dependent')"
    sql += " ORDER BY t.disclosure_date DESC, t.trade_date DESC"
    cur.execute(sql, params)
    return cur.fetchall()


def upsert_trade_diagnostics(
    conn: sqlite3.Connection,
    trade_id: int,
    diagnostics: Dict,
) -> int:
    """
    Idempotent insert/update on trade_diagnostics keyed by trade_id.
    Replaces any existing row for that trade_id (last write wins).

    diagnostics dict can include any of the columns defined in db_init.py
    SCHEMA for trade_diagnostics. Missing keys are written as NULL.
    """
    cur = conn.cursor()
    # Delete any existing row for this trade
    cur.execute("DELETE FROM trade_diagnostics WHERE trade_id = ?", (trade_id,))

    # Build the column list dynamically from the diagnostics dict, but only
    # include columns that actually exist in the schema. We hard-code the
    # known column list to avoid surprises.
    schema_cols = [
        "trade_id",
        "hist_range_45d", "realized_vol_60d", "sigma_move_val",
        "actual_price_move", "rsi", "iv_percentile", "iv_expansion",
        "volume_spike_detected",
        "earnings_occurred", "actual_earnings_move", "implied_earnings_move",
        "legislative_event_occurred", "legislative_event_detail",
        "news_catalyst_fired", "news_day_move",
        "sector_etf_move", "sector_vol_60d",
        "threshold_a1", "threshold_a2", "threshold_a3",
        "threshold_b1", "threshold_b3", "threshold_b4",
        "checks_fired", "verdict",
        "outcome_type", "outcome_pnl_30d", "outcome_pnl_60d", "outcome_pnl_90d",
        "evaluated_at",
    ]
    values = []
    placeholders = []
    cols = []
    for col in schema_cols:
        if col == "trade_id":
            cols.append(col)
            placeholders.append("?")
            values.append(trade_id)
        elif col in diagnostics:
            cols.append(col)
            placeholders.append("?")
            v = diagnostics[col]
            # JSON-serialize lists/dicts
            if isinstance(v, (list, dict)):
                v = json.dumps(v)
            values.append(v)

    if "evaluated_at" not in diagnostics:
        cols.append("evaluated_at")
        placeholders.append("?")
        values.append(now_iso())

    sql = f"INSERT INTO trade_diagnostics ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
    cur.execute(sql, values)
    conn.commit()
    return cur.lastrowid or -1


def upsert_recommendation(
    conn: sqlite3.Connection,
    trade_id: int,
    rec: Dict,
) -> int:
    """
    Idempotent insert/update on recommendations keyed by trade_id.
    Replaces any existing recommendation for the trade.

    rec dict columns (any subset):
        signal_tier, option_type, strike, expiry, dte,
        delta, gamma, theta, vega, iv,
        bid, ask, mid,
        entry_timestamp, thesis, bear_case, snapshot_caveat
    """
    cur = conn.cursor()
    cur.execute("DELETE FROM recommendations WHERE trade_id = ?", (trade_id,))

    schema_cols = [
        "trade_id",
        "signal_tier", "option_type", "strike", "expiry", "dte",
        "delta", "gamma", "theta", "vega", "iv",
        "bid", "ask", "mid",
        "entry_timestamp", "thesis", "bear_case", "snapshot_caveat",
    ]
    values = []
    placeholders = []
    cols = []
    for col in schema_cols:
        if col == "trade_id":
            cols.append(col)
            placeholders.append("?")
            values.append(trade_id)
        elif col in rec:
            cols.append(col)
            placeholders.append("?")
            values.append(rec[col])

    if "entry_timestamp" not in rec:
        cols.append("entry_timestamp")
        placeholders.append("?")
        values.append(now_iso())

    sql = f"INSERT INTO recommendations ({', '.join(cols)}) VALUES ({', '.join(placeholders)})"
    cur.execute(sql, values)
    conn.commit()
    return cur.lastrowid or -1


def update_trade_pipeline_columns(
    conn: sqlite3.Connection,
    trade_id: int,
    alignment_multiplier: Optional[float] = None,
    owd_score_a: Optional[int] = None,
    owd_score_b: Optional[int] = None,
    owd_total: Optional[int] = None,
    owd_verdict: Optional[str] = None,
    forward_catalyst: Optional[str] = None,
    clustering_count: Optional[int] = None,
    cross_party_cluster: Optional[int] = None,
    final_signal_tier: Optional[str] = None,
    skip_reason: Optional[str] = None,
) -> None:
    """
    Update the denormalized pipeline-output columns on a trades row.
    Called by daily_signal.py after scoring a trade so other agents
    (Deep-Dive, Weekly Deep) can read tier info directly from trades.
    """
    updates = {}
    if alignment_multiplier is not None:
        updates["alignment_multiplier"] = alignment_multiplier
    if owd_score_a is not None:
        updates["owd_score_a"] = owd_score_a
    if owd_score_b is not None:
        updates["owd_score_b"] = owd_score_b
    if owd_total is not None:
        updates["owd_total"] = owd_total
    if owd_verdict is not None:
        updates["owd_verdict"] = owd_verdict
    if forward_catalyst is not None:
        updates["forward_catalyst"] = forward_catalyst
    if clustering_count is not None:
        updates["clustering_count"] = clustering_count
    if cross_party_cluster is not None:
        updates["cross_party_cluster"] = cross_party_cluster
    if final_signal_tier is not None:
        updates["final_signal_tier"] = final_signal_tier
    if skip_reason is not None:
        updates["skip_reason"] = skip_reason

    if not updates:
        return

    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    set_clause += ", last_updated = ?"
    values = list(updates.values()) + [now_iso(), trade_id]
    cur = conn.cursor()
    cur.execute(f"UPDATE trades SET {set_clause} WHERE id = ?", values)
    conn.commit()


# ---------------------------------------------------------------------------
# Committee mappings
# ---------------------------------------------------------------------------

def committees_for_ticker(conn: sqlite3.Connection, ticker: str) -> List[str]:
    """Return all committees a ticker maps to (override list first, then sector)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT committee FROM committee_mappings WHERE ticker = ? AND multi_jurisdiction = 1",
        (ticker.upper(),),
    )
    overrides = [r["committee"] for r in cur.fetchall()]
    if overrides:
        return overrides
    return []


def committees_for_sector(conn: sqlite3.Connection, sector: str) -> List[str]:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT committee FROM committee_mappings WHERE sector = ?", (sector,))
    return [r["committee"] for r in cur.fetchall()]


def is_mega_cap_override(conn: sqlite3.Connection, ticker: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM committee_mappings WHERE ticker = ? AND multi_jurisdiction = 1 LIMIT 1",
        (ticker.upper(),),
    )
    return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Tunable parameters
# ---------------------------------------------------------------------------

def get_param(conn: sqlite3.Connection, name: str, default: Optional[float] = None) -> Optional[float]:
    cur = conn.cursor()
    cur.execute("SELECT value FROM tunable_parameters WHERE constant_name = ?", (name,))
    row = cur.fetchone()
    return row["value"] if row else default


def set_param(
    conn: sqlite3.Connection,
    name: str,
    value: float,
    rationale: str = "",
    approval_status: str = "approved",
) -> None:
    """Update a tunable param and append to the changelog."""
    cur = conn.cursor()
    ts = now_iso()
    old = get_param(conn, name)
    cur.execute(
        """
        INSERT INTO tunable_parameters (constant_name, value, last_updated)
        VALUES (?, ?, ?)
        ON CONFLICT(constant_name) DO UPDATE SET value = excluded.value, last_updated = excluded.last_updated
        """,
        (name, value, ts),
    )
    cur.execute(
        """
        INSERT INTO parameter_changelog
            (timestamp, constant_name, old_value, new_value, rationale, approval_status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ts, name, old, value, rationale, approval_status),
    )
    conn.commit()


def all_params(conn: sqlite3.Connection) -> Dict[str, float]:
    cur = conn.cursor()
    cur.execute("SELECT constant_name, value FROM tunable_parameters")
    return {row["constant_name"]: row["value"] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Raw source archival
# ---------------------------------------------------------------------------

def log_raw_source(
    conn: sqlite3.Connection,
    source: str,
    request_url: Optional[str],
    raw_response: str,
    parsed_count: int = 0,
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sources_raw (source, fetched_at, request_url, raw_response, parsed_count)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source, now_iso(), request_url, raw_response, parsed_count),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# CLI for ad-hoc queries
# ---------------------------------------------------------------------------

def _print_rows(rows: Iterable[sqlite3.Row], cols: Optional[List[str]] = None) -> None:
    rows = list(rows)
    if not rows:
        print("(no rows)")
        return
    if cols is None:
        cols = list(rows[0].keys())
    widths = {c: max(len(c), max((len(str(r[c] or "")) for r in rows), default=0)) for c in cols}
    header = " | ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for r in rows:
        print(" | ".join(str(r[c] or "").ljust(widths[c]) for c in cols))


def main():
    p = argparse.ArgumentParser(description="DB query helper")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("params", help="List all tunable parameters")

    pol = sub.add_parser("politicians", help="List politicians")
    pol.add_argument("--tier", choices=["core", "watchlist", "probationary", "candidate"])

    tr = sub.add_parser("trades", help="Query trades")
    tr.add_argument("--ticker")
    tr.add_argument("--politician")
    tr.add_argument("--since")
    tr.add_argument("--until")
    tr.add_argument("--type", dest="transaction_type", choices=["buy", "sell", "exchange"])
    tr.add_argument("--tier", dest="signal_tier")
    tr.add_argument("--limit", type=int, default=20)

    mp = sub.add_parser("mappings", help="Show committee mappings")
    mp.add_argument("--ticker")
    mp.add_argument("--sector")

    ld = sub.add_parser("latest-disclosure", help="Print most recent disclosure_date in trades table")
    ld.add_argument("--source", help="Filter by source (LIKE match), e.g. 'house_efd'")

    args = p.parse_args()
    if not args.cmd:
        p.print_help()
        return

    conn = connect()

    if args.cmd == "params":
        rows = conn.execute("SELECT constant_name, value, description, last_updated FROM tunable_parameters ORDER BY constant_name").fetchall()
        _print_rows(rows)
    elif args.cmd == "politicians":
        rows = list_politicians(conn, tier=args.tier)
        _print_rows(rows, ["id", "name", "chamber", "party", "roster_tier"])
    elif args.cmd == "trades":
        rows = query_trades(
            conn,
            ticker=args.ticker,
            politician_name=args.politician,
            since=args.since,
            until=args.until,
            transaction_type=args.transaction_type,
            signal_tier=args.signal_tier,
            limit=args.limit,
        )
        _print_rows(rows, ["id", "politician_name", "ticker", "trade_date", "transaction_type", "amount_range", "final_signal_tier"])
    elif args.cmd == "mappings":
        cur = conn.cursor()
        if args.ticker:
            cur.execute(
                "SELECT sector, committee, multi_jurisdiction, ticker, rationale FROM committee_mappings WHERE ticker = ?",
                (args.ticker.upper(),),
            )
        elif args.sector:
            cur.execute(
                "SELECT sector, committee, multi_jurisdiction, ticker, rationale FROM committee_mappings WHERE sector = ?",
                (args.sector,),
            )
        else:
            cur.execute("SELECT sector, committee, multi_jurisdiction, ticker, rationale FROM committee_mappings ORDER BY sector, committee")
        _print_rows(cur.fetchall(), ["sector", "committee", "multi_jurisdiction", "ticker", "rationale"])
    elif args.cmd == "latest-disclosure":
        latest = get_latest_disclosure_date(conn, source=args.source)
        if latest is None:
            print("(no trades in DB)")
        else:
            print(latest)

    conn.close()


if __name__ == "__main__":
    main()
