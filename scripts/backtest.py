#!/usr/bin/env python3
"""
backtest.py — Phase 0 backtest engine for roster generation.

Reads historical trades from the SQLite DB, filters to committee-aligned trades,
computes hit rate + return-vs-SPY at 60-day horizon, applies the pass bar from
specs/01-universe.md, and writes the resulting roster back to the politicians
table with appropriate roster_tier flags.

Usage:
    python3 scripts/backtest.py --lookback 5      # 5-year backtest (default)
    python3 scripts/backtest.py --report-only     # don't write to DB
    python3 scripts/backtest.py --min-trades 5    # min N for ranking eligibility
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import db
import sectors

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

# Pass bar from specs/01-universe.md
PASS_BAR_SPY_EXCESS = 5.0       # %
PASS_BAR_HIT_RATE = 0.55        # 55%
ROSTER_FLOOR = 15
HOLDING_HORIZON_DAYS = 60

# Minimum committee-aligned sample size for each roster tier.
# Below MIN_TRADES_WATCHLIST, stats are statistical noise (1-4 trades can
# easily produce 100% hit rate by luck) — excluded from the roster entirely.
MIN_TRADES_CORE = 10
MIN_TRADES_WATCHLIST = 5

# Recency weights (months → multiplier)
RECENCY_WEIGHTS = [
    (12,  1.00),
    (24,  0.70),
    (36,  0.50),
    (48,  0.30),
    (60,  0.15),
]


# ---------------------------------------------------------------------------
# Price fetching with cache
# ---------------------------------------------------------------------------

_price_cache: Dict[str, Dict] = {}  # ticker → {date_iso: close}


def get_close(ticker: str, target_date: str) -> Optional[float]:
    """Return the close price on/after target_date (next trading day if needed)."""
    if not HAS_YF:
        return None
    if ticker not in _price_cache:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="6y", auto_adjust=True)
            if hist.empty:
                _price_cache[ticker] = {}
                return None
            _price_cache[ticker] = {
                idx.strftime("%Y-%m-%d"): float(row["Close"])
                for idx, row in hist.iterrows()
            }
        except Exception as e:
            print(f"  [yf error] {ticker}: {e}", file=sys.stderr)
            _price_cache[ticker] = {}
            return None

    cache = _price_cache[ticker]
    if not cache:
        return None
    sorted_dates = sorted(cache.keys())
    for d in sorted_dates:
        if d >= target_date:
            return cache[d]
    return None


def compute_excess_return(ticker: str, trade_date: str, days: int = 60) -> Optional[float]:
    """% return of ticker minus % return of SPY over `days` calendar days from trade_date."""
    try:
        td = datetime.strptime(trade_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    end_date = (td + timedelta(days=days)).strftime("%Y-%m-%d")

    p_entry = get_close(ticker, trade_date)
    p_exit = get_close(ticker, end_date)
    spy_entry = get_close("SPY", trade_date)
    spy_exit = get_close("SPY", end_date)

    if not all([p_entry, p_exit, spy_entry, spy_exit]):
        return None
    if p_entry == 0 or spy_entry == 0:
        return None

    stock_ret = (p_exit / p_entry - 1) * 100
    spy_ret = (spy_exit / spy_entry - 1) * 100
    return stock_ret - spy_ret


# ---------------------------------------------------------------------------
# Backtest core
# ---------------------------------------------------------------------------

def is_aligned(conn, ticker: str, sector: Optional[str]) -> bool:
    """
    Phase-0 simplified alignment check: a trade is committee-aligned if
      (a) the ticker is on the mega-cap override list, OR
      (b) the trade has an explicit sector that maps to a committee, OR
      (c) yfinance-derived sector for the ticker maps to a committee.

    (c) is the new fallback — when ingest didn't classify sector (as in v1
    PTR-only ingestion), we lazy-classify via yfinance and cache the result.

    Time-varying committee membership (per-trade-date committee history) is a
    Phase-1 enhancement once we have the politicians.committee_history JSON
    populated. Until then we use the broader filter to bootstrap the dataset.
    """
    if db.is_mega_cap_override(conn, ticker):
        return True
    if sector and db.committees_for_sector(conn, sector):
        return True
    # Lazy classification via yfinance
    classified = sectors.classify_ticker(ticker)
    if classified and db.committees_for_sector(conn, classified):
        return True
    return False


def recency_weight(trade_date: str) -> float:
    """Return the recency multiplier for a trade based on age vs today."""
    try:
        td = datetime.strptime(trade_date, "%Y-%m-%d").date()
    except ValueError:
        return 0.15
    age_months = (datetime.utcnow().date() - td).days / 30
    for cap, mult in RECENCY_WEIGHTS:
        if age_months <= cap:
            return mult
    return 0.0


def evaluate_politician(conn, name: str, lookback_years: int, min_trades: int) -> Optional[Dict]:
    """Run the backtest for one politician. Returns a stats dict or None."""
    cutoff = (datetime.utcnow() - timedelta(days=lookback_years * 365)).strftime("%Y-%m-%d")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, ticker, sector, trade_date, transaction_type, amount_range, trader_tag
        FROM trades
        WHERE politician_name = ?
          AND trade_date >= ?
          AND transaction_type = 'buy'
          AND trader_tag = 'member_direct'
        ORDER BY trade_date ASC
        """,
        (name, cutoff),
    )
    rows = cur.fetchall()
    if not rows:
        return None

    aligned_results: List[Tuple[float, float]] = []  # (excess_ret, recency_weight)
    total_aligned = 0
    for r in rows:
        if not is_aligned(conn, r["ticker"], r["sector"]):
            continue
        total_aligned += 1
        excess = compute_excess_return(r["ticker"], r["trade_date"], days=HOLDING_HORIZON_DAYS)
        if excess is None:
            continue
        aligned_results.append((excess, recency_weight(r["trade_date"])))

    if total_aligned == 0:
        return None

    n = len(aligned_results)
    if n == 0:
        return {
            "name": name,
            "n_aligned_trades": total_aligned,
            "n_with_pricedata": 0,
            "skip": "no_price_data",
        }

    wins = sum(1 for ex, _ in aligned_results if ex > 0)
    avg_excess = sum(ex for ex, _ in aligned_results) / n
    hit_rate = wins / n

    # Recency-weighted
    total_weight = sum(w for _, w in aligned_results) or 1e-9
    rec_avg_excess = sum(ex * w for ex, w in aligned_results) / total_weight
    rec_wins_weight = sum(w for ex, w in aligned_results if ex > 0)
    rec_hit_rate = rec_wins_weight / total_weight

    return {
        "name": name,
        "n_aligned_trades": total_aligned,
        "n_with_pricedata": n,
        "hit_rate": hit_rate,
        "avg_excess_pct": avg_excess,
        "rec_hit_rate": rec_hit_rate,
        "rec_avg_excess_pct": rec_avg_excess,
        "wins": wins,
    }


def classify(stats: Dict) -> str:
    """
    Apply the pass bar + minimum sample size gate:
      - core: N >= MIN_TRADES_CORE AND passes both overall AND recency
      - watchlist (fading edge): N >= MIN_TRADES_WATCHLIST, passes overall, fails recency
      - watchlist: N >= MIN_TRADES_WATCHLIST, fails the return/hit-rate bar
      - insufficient_sample: N < MIN_TRADES_WATCHLIST (stats are noise)
    """
    if stats.get("skip"):
        return "candidate"
    n = stats["n_with_pricedata"]
    if n < MIN_TRADES_WATCHLIST:
        return "insufficient_sample"

    overall_pass = (
        stats["avg_excess_pct"] >= PASS_BAR_SPY_EXCESS
        and stats["hit_rate"] >= PASS_BAR_HIT_RATE
    )
    recent_pass = (
        stats["rec_avg_excess_pct"] >= PASS_BAR_SPY_EXCESS
        and stats["rec_hit_rate"] >= PASS_BAR_HIT_RATE
    )
    if overall_pass and recent_pass and n >= MIN_TRADES_CORE:
        return "core"
    if overall_pass and recent_pass and n < MIN_TRADES_CORE:
        return "watchlist"  # passes stats bar but sample too small for core
    if overall_pass and not recent_pass:
        return "watchlist"  # fading edge
    return "watchlist"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def format_report(results: List[Dict], floored_to: int = 0) -> str:
    """Pretty-print the backtest results as a markdown report."""
    lines = []
    lines.append("# Backtest Report — Roster Generation\n")
    lines.append(f"Generated: {datetime.utcnow().isoformat()}\n")
    lines.append(
        f"**Pass bar:** ≥{PASS_BAR_SPY_EXCESS}% excess vs SPY AND "
        f"≥{PASS_BAR_HIT_RATE*100:.0f}% hit rate, both overall AND recency-weighted"
    )
    lines.append(f"**Holding horizon:** {HOLDING_HORIZON_DAYS} days")
    lines.append(
        f"**Sample size gate:** core tier requires N≥{MIN_TRADES_CORE}, "
        f"watchlist requires N≥{MIN_TRADES_WATCHLIST}. "
        f"Below that = insufficient_sample (statistics are noise).\n"
    )

    if floored_to is not None and floored_to < ROSTER_FLOOR:
        lines.append(
            f"⚠ Floor: only {floored_to} politicians passed the bar. "
            f"The 'watchlist' tier below represents the next-best eligible candidates.\n"
        )

    def fmt_row(r):
        return (
            f"| {r.get('_tier','—')} | {r['name']} | {r['n_with_pricedata']} | "
            f"{r['hit_rate']*100:.0f}% | {r['avg_excess_pct']:+.1f}% | "
            f"{r['rec_hit_rate']*100:.0f}% | {r['rec_avg_excess_pct']:+.1f}% |"
        )

    valid = [r for r in results if r and not r.get("skip")]
    core = sorted(
        [r for r in valid if r["_tier"] == "core"],
        key=lambda r: (-r.get("rec_avg_excess_pct", 0), -r.get("rec_hit_rate", 0)),
    )
    watchlist = sorted(
        [r for r in valid if r["_tier"] == "watchlist"],
        key=lambda r: (-r.get("rec_avg_excess_pct", 0), -r.get("rec_hit_rate", 0)),
    )
    insufficient = sorted(
        [r for r in valid if r["_tier"] == "insufficient_sample"],
        key=lambda r: (-r.get("rec_avg_excess_pct", 0), -r.get("n_with_pricedata", 0)),
    )

    lines.append(f"## Core Roster ({len(core)} politicians)\n")
    if core:
        lines.append("| Tier | Name | N | Hit% | Excess% | Rec Hit% | Rec Excess% |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in core:
            lines.append(fmt_row(r))
    else:
        lines.append("*None cleared the core tier.*")
    lines.append("")

    lines.append(f"## Watchlist ({len(watchlist)} politicians)\n")
    if watchlist:
        lines.append("| Tier | Name | N | Hit% | Excess% | Rec Hit% | Rec Excess% |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in watchlist:
            lines.append(fmt_row(r))
    else:
        lines.append("*Empty.*")
    lines.append("")

    if insufficient:
        lines.append(
            f"## Insufficient Sample ({len(insufficient)} politicians — excluded from roster)\n"
        )
        lines.append(
            f"*These politicians have fewer than {MIN_TRADES_WATCHLIST} "
            f"committee-aligned trades, so their hit rate and excess return "
            f"are statistically meaningless. Listed here for transparency only.*\n"
        )
        lines.append("| Name | N | Hit% | Excess% |")
        lines.append("|---|---|---|---|")
        for r in insufficient:
            lines.append(
                f"| {r['name']} | {r['n_with_pricedata']} | "
                f"{r['hit_rate']*100:.0f}% | {r['avg_excess_pct']:+.1f}% |"
            )
        lines.append("")

    skipped = [r for r in results if r and r.get("skip")]
    if skipped:
        lines.append(f"\n## Skipped ({len(skipped)})")
        for r in skipped:
            lines.append(f"- {r['name']}: {r['skip']} (n_aligned_trades={r['n_aligned_trades']})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def write_roster(conn, results: List[Dict]) -> int:
    """Write classified results back to politicians table. Returns count written."""
    n = 0
    for r in results:
        if not r:
            continue
        tier = r.get("_tier", "candidate")
        try:
            db.upsert_politician(
                conn,
                name=r["name"],
                chamber=r.get("chamber") or "unknown",
                roster_tier=tier,
            )
            n += 1
        except Exception as e:
            print(f"  [persist error] {r['name']}: {e}", file=sys.stderr)
    return n


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Phase 0 backtest engine")
    p.add_argument("--lookback", type=int, default=5, help="Lookback years")
    p.add_argument("--min-trades", type=int, default=5, help="Min committee-aligned trades for ranking")
    p.add_argument("--report-only", action="store_true", help="Don't write roster to DB")
    p.add_argument("--limit", type=int, help="Cap politicians evaluated (debug)")
    p.add_argument("--out", help="Write report to this file")
    args = p.parse_args()

    if not HAS_YF:
        print("ERROR: yfinance not installed. pip install yfinance", file=sys.stderr)
        sys.exit(1)

    conn = db.connect()
    cur = conn.cursor()
    # Order by trade count so --limit focuses on most-active traders.
    cur.execute(
        """
        SELECT politician_name, COUNT(*) AS n FROM trades
        WHERE transaction_type = 'buy' AND trader_tag = 'member_direct'
        GROUP BY politician_name
        ORDER BY n DESC
        """
    )
    name_counts = cur.fetchall()
    names = [r["politician_name"] for r in name_counts]
    if args.limit:
        names = names[: args.limit]
        print(f"Top {args.limit} politicians by buy count:")
        for r in name_counts[: args.limit]:
            print(f"  {r['politician_name']}: {r['n']} buys")

    print(f"Evaluating {len(names)} politicians (lookback={args.lookback}yr, min-trades={args.min_trades})")
    results = []
    t0 = time.time()
    for i, name in enumerate(names):
        stats = evaluate_politician(conn, name, args.lookback, args.min_trades)
        if stats and not stats.get("skip"):
            stats["_tier"] = classify(stats)
        results.append(stats)
        if (i + 1) % 5 == 0:
            print(f"  [{i+1}/{len(names)}] elapsed {time.time()-t0:.0f}s")

    valid = [r for r in results if r and not r.get("skip")]
    # Roster-eligible = anyone with enough committee-aligned trades.
    # Insufficient_sample politicians are reported but never join the roster.
    eligible = [r for r in valid if r["_tier"] != "insufficient_sample"]
    passers = [r for r in eligible if r["_tier"] == "core"]

    floored_to = 0
    if len(passers) < ROSTER_FLOOR:
        # Apply floor: promote top watchlist-eligible candidates by hit rate
        # until we reach the ROSTER_FLOOR. Never promote insufficient_sample.
        sorted_eligible = sorted(
            eligible, key=lambda r: (-r["hit_rate"], -r["avg_excess_pct"])
        )
        # (Floor only changes tier labeling in the report; the actual gate is
        # still "core" vs everything else. We mark this transparently.)
        floored_to = len(passers)

    report = format_report(results, floored_to=floored_to)
    print("\n" + report)

    if args.out:
        Path(args.out).write_text(report)
        print(f"\nReport written to {args.out}")

    if not args.report_only:
        n = write_roster(conn, valid)
        print(f"\nWrote {n} politicians to DB")

    conn.close()


if __name__ == "__main__":
    main()
