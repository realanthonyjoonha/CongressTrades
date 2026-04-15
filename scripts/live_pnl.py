#!/usr/bin/env python3
"""
live_pnl.py — Today's live P&L for Top 5 Congressional Traders.

Phase 2.4: the user wants the Daily Signal agent to "first use my Massive
Market Data MCP to track how the Top 5 Congressional Traders are doing on
the day and what kind of positions they are holding."

This module computes today's live price + intraday P&L for each of the
Top 5's currently-open positions. Uses the Python MMD client
(`scripts/mmd_client.py`) rather than the MMD MCP because the daily
agent runs as a subprocess pipeline where MCP access is fragile.

Caching: live prices change constantly, but this module caches for 5
minutes at `data/live_pnl_cache.json` so repeated daily runs in rapid
succession don't hammer MMD. A 5-minute TTL is a reasonable compromise
between freshness and rate-limit politeness.

Usage:
    from live_pnl import compute_top5_day_pnl, render_top5_live_section

    # Low-level: get today's P&L per position
    positions = compute_top5_day_pnl(conn)

    # Markdown rendering for the Daily Signal research pack
    md = render_top5_live_section(conn, chart_registry=reg)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import smart_money  # noqa: E402
import stock_metrics  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_PATH = BASE_DIR / "data" / "live_pnl_cache.json"
CACHE_TTL_MIN = 5

# How far back to look for "today's open" when computing intraday move.
# We use the most recent trading day's 1-minute bars from MMD.


# ---------------------------------------------------------------------------
# MMD client (lazy)
# ---------------------------------------------------------------------------

_mmd_client = None


def _get_mmd_client():
    """Lazy-init MMD client. Returns None if API key missing or import fails."""
    global _mmd_client
    if _mmd_client is not None:
        return _mmd_client
    try:
        from mmd_client import MMDClient
        client = MMDClient()
        if not client.api_key:
            print(
                "  [live_pnl] MMD API key not configured — skipping live P&L",
                file=sys.stderr,
            )
            return None
        _mmd_client = client
        return client
    except Exception as e:
        print(f"  [live_pnl] MMD client init failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

def _load_cache() -> Optional[Dict]:
    """Load cached P&L if fresh (<5 min old). Returns None otherwise."""
    if not CACHE_PATH.exists():
        return None
    try:
        with open(CACHE_PATH) as f:
            cached = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    ts = cached.get("computed_at")
    if not ts:
        return None
    try:
        computed_dt = datetime.fromisoformat(ts.replace("Z", ""))
    except ValueError:
        return None
    age_sec = (datetime.utcnow() - computed_dt).total_seconds()
    if age_sec > CACHE_TTL_MIN * 60:
        return None
    return cached


def _save_cache(payload: Dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(payload, f, indent=2, default=str)
    except OSError as e:
        print(f"  [live_pnl] cache save failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# MMD price fetching helpers
# ---------------------------------------------------------------------------

def _latest_close_via_mmd(ticker: str, mmd_client) -> Optional[Dict]:
    """
    Fetch MMD aggregate bars for the last 5 trading days and return the
    most recent bar (today's close if market is closed; yesterday if it
    hasn't opened yet).

    Returns dict with {close, open, high, low, volume, timestamp_ms} or None.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        bars = mmd_client.get_stock_aggregates(
            ticker.upper(),
            from_date=start,
            to_date=today,
            timespan="day",
            limit=10,
        )
    except Exception as e:
        print(f"  [live_pnl] MMD aggregate fetch failed for {ticker}: {e}",
              file=sys.stderr)
        return None
    if not bars:
        return None
    # Last bar is the latest
    return bars[-1]


def _prev_close_via_mmd(ticker: str, mmd_client) -> Optional[float]:
    """
    Get the previous trading day's close via MMD. Used for intraday %
    move baseline (today's move relative to yesterday's close).
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")
    try:
        bars = mmd_client.get_stock_aggregates(
            ticker.upper(),
            from_date=start,
            to_date=today,
            timespan="day",
            limit=10,
        )
    except Exception:
        return None
    if not bars or len(bars) < 2:
        return None
    return bars[-2].get("close")


# ---------------------------------------------------------------------------
# Per-position P&L computation
# ---------------------------------------------------------------------------

def compute_position_day_pnl(
    position: Dict,
    mmd_client,
) -> Optional[Dict]:
    """
    Given an open position (from smart_money.get_open_positions) +
    an MMD client, compute today's price + intraday move + since-entry
    performance vs SPY.

    Returns dict with:
        ticker, entry_price, today_close, today_open, today_move_pct,
        since_entry_pct, since_entry_vs_spy_pct, trade_date, days_remaining,
        amount_range
    or None on failure.
    """
    ticker = position.get("ticker")
    entry_price = position.get("entry_price")
    if not ticker or not entry_price:
        return None

    bar = _latest_close_via_mmd(ticker, mmd_client)
    if not bar:
        return None

    today_close = bar.get("close")
    today_open = bar.get("open")
    if today_close is None:
        return None

    prev_close = _prev_close_via_mmd(ticker, mmd_client)

    today_move_pct = None
    if prev_close and prev_close > 0:
        today_move_pct = (today_close / prev_close - 1) * 100

    # Since-entry (use MMD close vs recorded entry, not the yfinance
    # entry in position — MMD is more reliable for equities)
    since_entry_pct = (today_close / entry_price - 1) * 100 if entry_price > 0 else None

    # SPY benchmark
    spy_bar = _latest_close_via_mmd("SPY", mmd_client)
    spy_today_close = spy_bar.get("close") if spy_bar else None

    since_entry_vs_spy = None
    if since_entry_pct is not None and spy_today_close:
        # SPY's since-trade-date return via yfinance (already computed)
        spy_move = stock_metrics.compute_actual_move("SPY", position.get("trade_date"))
        if spy_move and spy_move.get("signed_pct_move") is not None:
            since_entry_vs_spy = since_entry_pct - (spy_move["signed_pct_move"] * 100)

    return {
        "ticker": ticker,
        "entry_price": entry_price,
        "today_close": today_close,
        "today_open": today_open,
        "prev_close": prev_close,
        "today_move_pct": today_move_pct,
        "since_entry_pct": since_entry_pct,
        "since_entry_vs_spy_pct": since_entry_vs_spy,
        "trade_date": position.get("trade_date"),
        "days_remaining": position.get("days_remaining"),
        "amount_range": position.get("amount_range"),
    }


# ---------------------------------------------------------------------------
# Top 5 roll-up
# ---------------------------------------------------------------------------

def compute_top5_day_pnl(
    conn,
    force_refresh: bool = False,
) -> List[Dict]:
    """
    For each of the Top 5 Congressional Traders, compute today's live
    P&L on each of their currently-open positions (via MMD).

    Returns list of dicts, one per politician:
        {
            name, rank, rec_hit_rate, rec_avg_excess_pct,
            positions: [ {ticker, today_close, today_move_pct, ...}, ... ],
            has_positions: bool,
            n_positions: int,
            best_today: {ticker, today_move_pct},
            worst_today: {ticker, today_move_pct},
        }
    """
    if not force_refresh:
        cached = _load_cache()
        if cached and isinstance(cached.get("top5"), list):
            return cached["top5"]

    mmd_client = _get_mmd_client()
    if mmd_client is None:
        print("  [live_pnl] no MMD client available — returning empty list",
              file=sys.stderr)
        return []

    top = smart_money.get_top_performers(conn)
    if not top:
        return []

    out: List[Dict] = []
    for perf in top:
        name = perf.get("name")
        if not name:
            continue
        positions = smart_money.get_open_positions(conn, name)
        position_pnls: List[Dict] = []
        for p in positions:
            day = compute_position_day_pnl(p, mmd_client)
            if day:
                position_pnls.append(day)

        best_today = None
        worst_today = None
        if position_pnls:
            scored = [p for p in position_pnls if p.get("today_move_pct") is not None]
            if scored:
                scored.sort(key=lambda p: -p["today_move_pct"])
                best_today = {
                    "ticker": scored[0]["ticker"],
                    "today_move_pct": scored[0]["today_move_pct"],
                }
                worst_today = {
                    "ticker": scored[-1]["ticker"],
                    "today_move_pct": scored[-1]["today_move_pct"],
                }

        out.append({
            "name": name,
            "rank": perf.get("rank"),
            "rec_hit_rate": perf.get("rec_hit_rate"),
            "rec_avg_excess_pct": perf.get("rec_avg_excess_pct"),
            "n_positions": len(position_pnls),
            "has_positions": len(position_pnls) > 0,
            "positions": position_pnls,
            "best_today": best_today,
            "worst_today": worst_today,
        })

    _save_cache({
        "computed_at": datetime.utcnow().isoformat() + "Z",
        "top5": out,
    })

    return out


# ---------------------------------------------------------------------------
# Markdown rendering for daily_signal research pack
# ---------------------------------------------------------------------------

def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def render_top5_live_section(
    conn,
    force_refresh: bool = False,
    chart_registry=None,
) -> str:
    """
    Render the "Top 5 Live P&L Today" section as markdown.

    Structure:
      - Header + intro
      - For each Top 5 politician with ≥1 open position:
          sub-header with name + rank + historical track record
          compact table of today's prices and moves per open position
          best/worst today
      - Skip politicians with 0 open positions (they're noted in Smart Money already)
      - Footer timestamp for freshness
    """
    top5 = compute_top5_day_pnl(conn, force_refresh=force_refresh)

    lines: List[str] = []
    lines.append("## Top 5 Live P&L Today\n")
    lines.append(
        "*Live intraday and since-entry P&L for every currently-open "
        "position held by the Top 5 Congressional Traders. Prices from "
        "Massive Market Data (5-min cache).*\n"
    )

    if not top5:
        lines.append("*Live P&L unavailable — MMD client not configured.*\n")
        return "\n".join(lines) + "\n"

    with_positions = [p for p in top5 if p.get("has_positions")]
    if not with_positions:
        lines.append(
            "*None of the Top 5 have currently-open positions. Nothing "
            "to track today.*\n"
        )
        return "\n".join(lines) + "\n"

    for politician in with_positions:
        name = politician.get("name")
        rank = politician.get("rank")
        rec_hit = politician.get("rec_hit_rate")
        rec_excess = politician.get("rec_avg_excess_pct")

        lines.append(f"### #{rank} {name}\n")

        track_record_parts = []
        if rec_hit is not None:
            track_record_parts.append(f"{rec_hit * 100:.0f}% hit rate")
        if rec_excess is not None:
            track_record_parts.append(f"{rec_excess:+.1f}% rec-wtd excess")
        if track_record_parts:
            lines.append(
                f"*Track record: {' · '.join(track_record_parts)}*\n"
            )

        lines.append("| Ticker | Today | Since Entry | vs SPY | Days Left | Amount |")
        lines.append("|---|---|---|---|---|---|")
        for pos in politician.get("positions", []):
            today_str = _fmt_pct(pos.get("today_move_pct"))
            since_str = _fmt_pct(pos.get("since_entry_pct"))
            vs_spy_str = _fmt_pct(pos.get("since_entry_vs_spy_pct"))
            lines.append(
                f"| {pos.get('ticker', '—')} "
                f"| {today_str} "
                f"| {since_str} "
                f"| {vs_spy_str} "
                f"| {pos.get('days_remaining', '—')}d "
                f"| {pos.get('amount_range', '—')} |"
            )

        best = politician.get("best_today")
        worst = politician.get("worst_today")
        if best and worst and best["ticker"] != worst["ticker"]:
            lines.append(
                f"\n*Best today: {best['ticker']} {_fmt_pct(best['today_move_pct'])}. "
                f"Worst: {worst['ticker']} {_fmt_pct(worst['today_move_pct'])}.*"
            )
        lines.append("")

    lines.append(
        f"*Prices as of {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} "
        f"(5-min cache).*\n"
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    conn = db.connect()

    if cmd == "refresh":
        print("[live_pnl] forcing refresh (bypasses cache)...")
        top5 = compute_top5_day_pnl(conn, force_refresh=True)
        print(f"\ncomputed P&L for {len(top5)} politicians")
        for p in top5:
            print(f"  #{p['rank']} {p['name']}: {p['n_positions']} positions")

    elif cmd == "show":
        md = render_top5_live_section(conn)
        print(md)

    elif cmd == "json":
        top5 = compute_top5_day_pnl(conn)
        print(_json.dumps(top5, indent=2, default=str))

    else:
        print("Usage: python3 live_pnl.py [show|refresh|json]")

    conn.close()
