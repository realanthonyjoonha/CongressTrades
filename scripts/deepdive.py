#!/usr/bin/env python3
"""
deepdive.py — Politician Deep-Dive analytical driver (Phase 2.1).

Phase A of the Deep-Dive agent. Pulls all trades for one politician from the
SQLite DB, computes hit rate, excess return vs SPY, sector concentration,
committee alignment, and timing patterns. Writes a "research pack" markdown
file that the LLM narrative layer reads in Phase B.

CLI:
    python3 scripts/deepdive.py "Josh Gottheimer" --out outputs/tmp/pack.md
    python3 scripts/deepdive.py "Mark Green" --lookback 5 --out pack.md

Design:
- All deterministic, quant-only output. No web searches, no narrative.
- Reuses evaluate_politician / compute_excess_return / is_aligned from
  backtest.py so the Deep-Dive's member_direct buy stats match the backtest
  exactly.
- Separates member_direct, spouse, and dependent trades into separate buckets
  per the spec ("Spouse trade performance (separate)").
- Writes a canonical structure that prompts/deepdive.md will consume.

Exits with code 0 on success, 1 on "politician not found" or empty research
pack, so the shell runner can fail fast before spawning Claude.
"""
from __future__ import annotations

import argparse
import os
import sys
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Local imports — same directory as backtest.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import backtest  # noqa: E402
import sectors  # noqa: E402
from ingest import canonical_politician_name  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOOKBACK_YEARS = 5


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_all_trades(
    conn, canonical_name: str, lookback_years: int
) -> List[dict]:
    """
    Pull every trade for this politician in the lookback window.
    Returns plain dicts (not sqlite3.Row) for easier manipulation.
    Includes all trader_tags and all transaction_types.
    """
    cutoff = (datetime.utcnow() - timedelta(days=lookback_years * 365)).strftime("%Y-%m-%d")
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, ticker, sector, trade_date, disclosure_date,
               transaction_type, amount_range, trader_tag, source
        FROM trades
        WHERE politician_name = ?
          AND trade_date >= ?
        ORDER BY trade_date ASC
        """,
        (canonical_name, cutoff),
    )
    return [dict(r) for r in cur.fetchall()]


def split_by_trader_tag(trades: List[dict]) -> Dict[str, List[dict]]:
    """Bucket trades into {member_direct, spouse, dependent, other}."""
    buckets: Dict[str, List[dict]] = defaultdict(list)
    for t in trades:
        tag = t.get("trader_tag") or "member_direct"
        buckets[tag].append(t)
    return dict(buckets)


# ---------------------------------------------------------------------------
# Stats computation — reuses backtest internals
# ---------------------------------------------------------------------------

def compute_stats_bucket(conn, trades: List[dict], label: str) -> dict:
    """
    Compute full stats for a subset of trades: total counts, committee
    alignment, hit rate, excess return, recency-weighted metrics.
    Only BUYS contribute to hit-rate math (sells don't have a meaningful
    "was this a good entry?" interpretation).
    """
    if not trades:
        return {"label": label, "empty": True}

    total = len(trades)
    buys = [t for t in trades if t["transaction_type"] == "buy"]
    sells = [t for t in trades if t["transaction_type"] == "sell"]
    exchanges = [t for t in trades if t["transaction_type"] == "exchange"]
    other = [t for t in trades if t["transaction_type"] not in ("buy", "sell", "exchange")]

    # Committee alignment on buys only (matches backtest logic)
    aligned_buys = [t for t in buys if backtest.is_aligned(conn, t["ticker"], t.get("sector"))]

    # Compute excess returns on aligned buys with available price data
    results: List[Tuple[float, float]] = []  # (excess, recency_weight)
    for t in aligned_buys:
        excess = backtest.compute_excess_return(
            t["ticker"], t["trade_date"], days=backtest.HOLDING_HORIZON_DAYS
        )
        if excess is None:
            continue
        results.append((excess, backtest.recency_weight(t["trade_date"])))

    n_scored = len(results)
    stats = {
        "label": label,
        "empty": False,
        "n_total": total,
        "n_buys": len(buys),
        "n_sells": len(sells),
        "n_exchanges": len(exchanges),
        "n_other": len(other),
        "n_aligned_buys": len(aligned_buys),
        "n_scored": n_scored,
        "align_pct": (len(aligned_buys) / len(buys) * 100) if buys else 0.0,
    }
    if n_scored == 0:
        stats["skip_reason"] = "no_price_data"
        return stats

    wins = sum(1 for ex, _ in results if ex > 0)
    stats["wins"] = wins
    stats["hit_rate"] = wins / n_scored
    stats["avg_excess_pct"] = sum(ex for ex, _ in results) / n_scored
    stats["median_excess_pct"] = statistics.median(ex for ex, _ in results)

    total_weight = sum(w for _, w in results) or 1e-9
    stats["rec_hit_rate"] = sum(w for ex, w in results if ex > 0) / total_weight
    stats["rec_avg_excess_pct"] = sum(ex * w for ex, w in results) / total_weight

    # Best / worst trades
    results_sorted = sorted(results, key=lambda x: -x[0])
    stats["best_excess_pct"] = results_sorted[0][0] if results_sorted else 0.0
    stats["worst_excess_pct"] = results_sorted[-1][0] if results_sorted else 0.0

    return stats


# ---------------------------------------------------------------------------
# Sector concentration
# ---------------------------------------------------------------------------

def sector_concentration(trades: List[dict]) -> List[dict]:
    """
    Count buys by sector (classified via sectors.classify_ticker if no sector
    column is set). Returns sorted list of {sector, n, midpoint_total}.
    Only buys are counted (sells distort the "where is capital flowing" picture).
    """
    buys = [t for t in trades if t["transaction_type"] == "buy"]
    sector_counts: Counter = Counter()
    sector_dollars: Dict[str, float] = defaultdict(float)

    for t in buys:
        sec = t.get("sector") or sectors.classify_ticker(t["ticker"]) or "unclassified"
        sector_counts[sec] += 1
        lo, hi = db.parse_amount_range(t.get("amount_range"))
        if lo and hi:
            sector_dollars[sec] += (lo + hi) / 2.0

    out = []
    for sec, n in sector_counts.most_common():
        out.append({
            "sector": sec,
            "n": n,
            "midpoint_total": sector_dollars.get(sec, 0.0),
        })
    return out


def top_tickers(trades: List[dict], n: int = 10) -> List[dict]:
    """
    Most-traded tickers by buy count. Returns list of
    {ticker, n_buys, last_trade_date, avg_excess_pct_or_none}.

    Optimization: pick the top-N by count FIRST, then only compute excess
    returns for those N tickers. Avoids fetching yfinance data for 200+
    long-tail tickers that will never appear in the output.
    """
    buys = [t for t in trades if t["transaction_type"] == "buy"]
    by_ticker: Dict[str, List[dict]] = defaultdict(list)
    for t in buys:
        by_ticker[t["ticker"]].append(t)

    # Rank by buy count, take top N, then compute excess for those only
    ranked = sorted(by_ticker.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:n]

    rows = []
    for ticker, ts in ranked:
        last_date = max(t["trade_date"] for t in ts)
        excesses = []
        for t in ts:
            ex = backtest.compute_excess_return(
                ticker, t["trade_date"], days=backtest.HOLDING_HORIZON_DAYS
            )
            if ex is not None:
                excesses.append(ex)
        rows.append({
            "ticker": ticker,
            "n_buys": len(ts),
            "last_trade_date": last_date,
            "avg_excess_pct": (sum(excesses) / len(excesses)) if excesses else None,
            "n_scored": len(excesses),
        })
    return rows


# ---------------------------------------------------------------------------
# Recent activity + timing
# ---------------------------------------------------------------------------

def recent_activity_windows(trades: List[dict]) -> dict:
    """Count trades in trailing 30/60/90/180-day windows and compute disclosure lag."""
    today = datetime.utcnow().date()

    def age_days(d_iso: str) -> Optional[int]:
        try:
            d = datetime.strptime(d_iso, "%Y-%m-%d").date()
            return (today - d).days
        except (ValueError, TypeError):
            return None

    def disc_lag(t: dict) -> Optional[int]:
        try:
            trade = datetime.strptime(t["trade_date"], "%Y-%m-%d").date()
            disc = datetime.strptime(t["disclosure_date"], "%Y-%m-%d").date()
            return (disc - trade).days
        except (ValueError, TypeError, KeyError):
            return None

    buckets = {30: 0, 60: 0, 90: 0, 180: 0}
    for t in trades:
        a = age_days(t["trade_date"])
        if a is None:
            continue
        for w in buckets:
            if a <= w:
                buckets[w] += 1

    lags = [d for d in (disc_lag(t) for t in trades) if d is not None and d >= 0]
    lag_stats = {}
    if lags:
        lag_stats = {
            "avg_disclosure_lag_days": round(sum(lags) / len(lags), 1),
            "median_disclosure_lag_days": statistics.median(lags),
            "max_disclosure_lag_days": max(lags),
            "min_disclosure_lag_days": min(lags),
            "n_with_lag": len(lags),
        }

    # Most recent trades (last 10)
    recent_trades = sorted(
        trades, key=lambda t: t.get("trade_date") or "", reverse=True
    )[:10]

    return {
        "windows": buckets,
        "lag": lag_stats,
        "most_recent_10": recent_trades,
    }


def committee_alignment_summary(conn, trades: List[dict]) -> dict:
    """
    Per-committee breakdown of the politician's buys. Returns counts keyed by
    committee ID (HPSCI, HASC, etc) showing how many of their buys fall into
    each jurisdiction.
    """
    buys = [t for t in trades if t["transaction_type"] == "buy"]
    committee_counts: Counter = Counter()
    aligned = 0
    off_committee = 0

    for t in buys:
        committees = db.committees_for_ticker(conn, t["ticker"])
        if not committees:
            sec = t.get("sector") or sectors.classify_ticker(t["ticker"])
            if sec:
                committees = db.committees_for_sector(conn, sec)
        if committees:
            aligned += 1
            for c in committees:
                committee_counts[c] += 1
        else:
            off_committee += 1

    return {
        "n_buys": len(buys),
        "n_aligned": aligned,
        "n_off_committee": off_committee,
        "align_pct": (aligned / len(buys) * 100) if buys else 0.0,
        "by_committee": committee_counts.most_common(),
    }


def timing_patterns(trades: List[dict]) -> dict:
    """Trades per quarter histogram + longest gap between trades."""
    if not trades:
        return {"empty": True}
    by_quarter: Counter = Counter()
    dates = []
    for t in trades:
        try:
            d = datetime.strptime(t["trade_date"], "%Y-%m-%d").date()
            q = f"{d.year}-Q{(d.month - 1) // 3 + 1}"
            by_quarter[q] += 1
            dates.append(d)
        except (ValueError, TypeError):
            continue

    dates.sort()
    longest_gap_days = 0
    if len(dates) >= 2:
        for a, b in zip(dates, dates[1:]):
            gap = (b - a).days
            if gap > longest_gap_days:
                longest_gap_days = gap

    return {
        "empty": False,
        "by_quarter": sorted(by_quarter.items()),
        "longest_gap_days": longest_gap_days,
        "first_trade_date": dates[0].strftime("%Y-%m-%d") if dates else None,
        "last_trade_date": dates[-1].strftime("%Y-%m-%d") if dates else None,
    }


# ---------------------------------------------------------------------------
# Research pack renderer
# ---------------------------------------------------------------------------

def _fmt_dollar(n: float) -> str:
    if n == 0:
        return "$0"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.0f}K"
    return f"${n:.0f}"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%"


def render_stats_bucket(s: dict) -> List[str]:
    if s.get("empty"):
        return [f"*No trades in this bucket.*"]
    lines = []
    lines.append(f"- **Total trades:** {s['n_total']} "
                 f"(buys: {s['n_buys']}, sells: {s['n_sells']}, "
                 f"exchanges: {s['n_exchanges']}, other: {s['n_other']})")
    lines.append(f"- **Committee-aligned buys:** {s['n_aligned_buys']} / {s['n_buys']} "
                 f"({s['align_pct']:.1f}%)")
    if s.get("skip_reason"):
        lines.append(f"- *Skipped return computation: {s['skip_reason']}*")
        return lines
    lines.append(f"- **Buys with price data (N for hit-rate):** {s['n_scored']}")
    lines.append(f"- **Hit rate (60d excess vs SPY):** "
                 f"{s['hit_rate']*100:.1f}% ({s['wins']}/{s['n_scored']})")
    lines.append(f"- **Average excess vs SPY:** {_fmt_pct(s['avg_excess_pct'])}")
    lines.append(f"- **Median excess vs SPY:** {_fmt_pct(s['median_excess_pct'])}")
    lines.append(f"- **Recency-weighted hit rate:** {s['rec_hit_rate']*100:.1f}%")
    lines.append(f"- **Recency-weighted excess:** {_fmt_pct(s['rec_avg_excess_pct'])}")
    lines.append(f"- **Best trade (excess):** {_fmt_pct(s['best_excess_pct'])}")
    lines.append(f"- **Worst trade (excess):** {_fmt_pct(s['worst_excess_pct'])}")
    return lines


def render_research_pack(
    politician_canonical: str,
    politician_record: Optional[dict],
    lookback_years: int,
    buckets: Dict[str, List[dict]],
    bucket_stats: Dict[str, dict],
    sector_rows: List[dict],
    ticker_rows: List[dict],
    recent: dict,
    align_summary: dict,
    timing: dict,
    all_trades_count: int,
) -> str:
    """Assemble the final research pack markdown."""
    lines = []
    lines.append(f"# Deep-Dive Research Pack: {politician_canonical}\n")
    lines.append(f"*Generated: {datetime.utcnow().isoformat()}Z  |  "
                 f"Lookback: {lookback_years} years  |  "
                 f"Holding horizon: {backtest.HOLDING_HORIZON_DAYS} days*\n")
    lines.append("This file is the deterministic data layer produced by "
                 "`scripts/deepdive.py`. The LLM narrative agent (`prompts/"
                 "deepdive.md`) reads this pack and synthesizes the final "
                 "report. **All numbers below are source of truth — do not "
                 "recompute.**\n")

    # --- Politician Record ---
    lines.append("## Politician Record\n")
    if politician_record:
        lines.append(f"- **Canonical name:** {politician_canonical}")
        lines.append(f"- **Chamber:** {politician_record.get('chamber') or '—'}")
        lines.append(f"- **Party:** {politician_record.get('party') or '—'}")
        lines.append(f"- **Roster tier:** {politician_record.get('roster_tier') or 'candidate'}")
    else:
        lines.append(f"- **Canonical name:** {politician_canonical}")
        lines.append("- *No row in `politicians` table (pre-Phase-2 rosters "
                     "don't populate this yet; trades are still queryable).*")
    lines.append(f"- **Trades in DB (lookback window):** {all_trades_count}")
    lines.append("")

    # --- Member trades ---
    lines.append("## Member Direct Trades\n")
    lines.extend(render_stats_bucket(bucket_stats.get("member_direct", {"empty": True})))
    lines.append("")

    # --- Spouse trades ---
    lines.append("## Spouse Trades\n")
    spouse_stats = bucket_stats.get("spouse", {"empty": True})
    if spouse_stats.get("empty"):
        lines.append("*No spouse trades in the lookback window.*")
    else:
        lines.extend(render_stats_bucket(spouse_stats))
        lines.append(
            "\n*Spouse trades are tracked separately because the "
            "STOCK-Act committee-alignment inference is weaker — a spouse "
            "is not the committee member. Use this bucket for context, not "
            "as a primary signal.*"
        )
    lines.append("")

    # --- Dependent trades ---
    dep_stats = bucket_stats.get("dependent", {"empty": True})
    if not dep_stats.get("empty"):
        lines.append("## Dependent Trades\n")
        lines.extend(render_stats_bucket(dep_stats))
        lines.append("")

    # --- Sector concentration ---
    lines.append("## Sector Concentration (member direct buys)\n")
    if sector_rows:
        lines.append("| Rank | Sector | Buys | Midpoint $ Total |")
        lines.append("|---|---|---|---|")
        for i, row in enumerate(sector_rows[:15], 1):
            lines.append(
                f"| {i} | {row['sector']} | {row['n']} | "
                f"{_fmt_dollar(row['midpoint_total'])} |"
            )
    else:
        lines.append("*No member-direct buys in the lookback window.*")
    lines.append("")

    # --- Top tickers ---
    lines.append("## Top 10 Most-Traded Tickers (member direct buys)\n")
    if ticker_rows:
        lines.append("| Rank | Ticker | Buys | Last Trade | Avg Excess (60d) |")
        lines.append("|---|---|---|---|---|")
        for i, row in enumerate(ticker_rows, 1):
            lines.append(
                f"| {i} | {row['ticker']} | {row['n_buys']} | "
                f"{row['last_trade_date']} | {_fmt_pct(row['avg_excess_pct'])} |"
            )
    else:
        lines.append("*No member-direct buys.*")
    lines.append("")

    # --- Committee alignment summary ---
    lines.append("## Committee Alignment Breakdown (all trader tags, buys)\n")
    lines.append(f"- **Total buys:** {align_summary['n_buys']}")
    lines.append(f"- **Aligned (at least one committee):** "
                 f"{align_summary['n_aligned']} ({align_summary['align_pct']:.1f}%)")
    lines.append(f"- **Off-committee:** {align_summary['n_off_committee']}")
    if align_summary["by_committee"]:
        lines.append("\n**By committee jurisdiction:**\n")
        lines.append("| Committee | Buy count |")
        lines.append("|---|---|")
        for committee, n in align_summary["by_committee"]:
            lines.append(f"| {committee} | {n} |")
    lines.append("")

    # --- Recent activity ---
    lines.append("## Recent Activity (all trader tags)\n")
    win = recent["windows"]
    lines.append(f"- **Last 30 days:** {win.get(30, 0)} trades")
    lines.append(f"- **Last 60 days:** {win.get(60, 0)} trades")
    lines.append(f"- **Last 90 days:** {win.get(90, 0)} trades")
    lines.append(f"- **Last 180 days:** {win.get(180, 0)} trades")
    if recent.get("lag"):
        lag = recent["lag"]
        lines.append(f"- **Average disclosure lag:** {lag['avg_disclosure_lag_days']} days "
                     f"(median {lag['median_disclosure_lag_days']}, "
                     f"range {lag['min_disclosure_lag_days']}–{lag['max_disclosure_lag_days']})")
    if recent["most_recent_10"]:
        lines.append("\n**Most recent 10 trades:**\n")
        lines.append("| Date | Ticker | Type | Tag | Amount | Disclosed |")
        lines.append("|---|---|---|---|---|---|")
        for t in recent["most_recent_10"]:
            lines.append(
                f"| {t['trade_date']} | {t['ticker']} | {t['transaction_type']} | "
                f"{t.get('trader_tag') or '—'} | {t.get('amount_range') or '—'} | "
                f"{t.get('disclosure_date') or '—'} |"
            )
    lines.append("")

    # --- Timing patterns ---
    lines.append("## Timing Patterns (all trader tags)\n")
    if timing.get("empty"):
        lines.append("*No trades to analyze.*")
    else:
        lines.append(f"- **First trade in window:** {timing['first_trade_date']}")
        lines.append(f"- **Last trade in window:** {timing['last_trade_date']}")
        lines.append(f"- **Longest gap between trades:** {timing['longest_gap_days']} days")
        if timing["by_quarter"]:
            lines.append("\n**Trades per quarter:**\n")
            lines.append("| Quarter | Trades |")
            lines.append("|---|---|")
            # Show last 12 quarters (3 years) to keep it digestible
            for q, n in timing["by_quarter"][-12:]:
                lines.append(f"| {q} | {n} |")
    lines.append("")

    # --- Footer with input for LLM ---
    lines.append("---")
    lines.append(
        "*End of research pack. The narrative agent should now perform 15–25 "
        "web searches to enrich these numbers with current committee work, "
        "recent statements, upcoming hearings, and any relevant news.*"
    )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Generate Deep-Dive research pack for one politician")
    ap.add_argument("politician", help='Politician name, e.g. "Josh Gottheimer"')
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_YEARS,
                    help=f"Years of history (default {DEFAULT_LOOKBACK_YEARS})")
    ap.add_argument("--out", type=str, default=None,
                    help="Output path for research pack markdown")
    args = ap.parse_args()

    canonical = canonical_politician_name(args.politician)
    if not canonical:
        print(f"[deepdive] ERROR: could not canonicalize politician name: {args.politician!r}",
              file=sys.stderr)
        sys.exit(1)
    print(f"[deepdive] politician: {args.politician!r} -> canonical: {canonical!r}")

    conn = db.connect()

    # Politician record (may not exist — that's OK)
    politician_record = None
    pol = db.get_politician(conn, canonical)
    if pol:
        politician_record = dict(pol)

    # Fetch all trades in the lookback window
    trades = fetch_all_trades(conn, canonical, args.lookback)
    print(f"[deepdive] fetched {len(trades)} trades for {canonical}")

    if not trades:
        print(f"[deepdive] ERROR: no trades in DB for {canonical!r}", file=sys.stderr)
        print(f"[deepdive] Suggestions: check spelling, run "
              f"`./run-agent.sh db-query \"SELECT DISTINCT politician_name "
              f"FROM trades WHERE politician_name LIKE '%{canonical.split()[-1]}%'\"`",
              file=sys.stderr)
        sys.exit(1)

    # Split by trader tag
    buckets = split_by_trader_tag(trades)
    print(f"[deepdive] trader_tag split: "
          f"{ {k: len(v) for k, v in buckets.items()} }")

    # Compute stats for each bucket
    bucket_stats = {}
    for tag in ("member_direct", "spouse", "dependent"):
        bucket_trades = buckets.get(tag, [])
        label = {
            "member_direct": "Member Direct",
            "spouse": "Spouse",
            "dependent": "Dependent",
        }[tag]
        print(f"[deepdive] computing stats: {label} ({len(bucket_trades)} trades)...")
        bucket_stats[tag] = compute_stats_bucket(conn, bucket_trades, label)

    # Sector concentration + top tickers on member_direct only
    member_trades = buckets.get("member_direct", [])
    print(f"[deepdive] computing sector concentration...")
    sector_rows = sector_concentration(member_trades)
    print(f"[deepdive] computing top tickers...")
    ticker_rows = top_tickers(member_trades, n=10)

    # Recent activity + timing across ALL trader tags
    print(f"[deepdive] computing recent activity + timing...")
    recent = recent_activity_windows(trades)
    align_summary = committee_alignment_summary(conn, trades)
    timing = timing_patterns(trades)

    # Render
    pack = render_research_pack(
        politician_canonical=canonical,
        politician_record=politician_record,
        lookback_years=args.lookback,
        buckets=buckets,
        bucket_stats=bucket_stats,
        sector_rows=sector_rows,
        ticker_rows=ticker_rows,
        recent=recent,
        align_summary=align_summary,
        timing=timing,
        all_trades_count=len(trades),
    )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(pack)
        print(f"[deepdive] wrote research pack: {out_path} ({len(pack):,} bytes)")
    else:
        sys.stdout.write(pack)

    # Persist any new price cache entries for future runs
    backtest.save_price_cache()


if __name__ == "__main__":
    main()
