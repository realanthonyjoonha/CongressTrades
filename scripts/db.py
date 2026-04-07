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

    conn.close()


if __name__ == "__main__":
    main()
