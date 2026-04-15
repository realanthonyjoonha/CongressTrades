#!/usr/bin/env python3
"""
smart_money.py — "Smart Money Watchlist" tracker for the Daily Signal.

Computes the top 5 politicians by recency-weighted excess return vs SPY
(from backtest.evaluate_politician) and tracks their currently-open
positions (trades within the 60-day holding window from trade_date).

The ranking rarely changes day-to-day, so it's cached to
data/top_performers.json with a 7-day TTL. Open positions are computed
fresh on every daily run because new trades drop into the window every
day.

Output is a compact markdown table rendered at the top of the Daily
Signal research pack (after TL;DR, before STRONG plays). The LLM is
instructed to embed the table verbatim in the final narrative.

Usage:
    from smart_money import smart_money_section

    pack_section_md = smart_money_section(conn)
    # Insert into the research pack after TL;DR

    # Or refresh the cache manually:
    python3 scripts/smart_money.py refresh
    python3 scripts/smart_money.py show
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import backtest  # noqa: E402
import stock_metrics  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_PATH = BASE_DIR / "data" / "top_performers.json"

# How long the ranking cache stays valid (7 days)
CACHE_TTL_DAYS = 7

# Minimum trades required to be eligible for the top-5 list.
# Matches backtest's MIN_TRADES_WATCHLIST so insufficient-sample
# politicians don't game the ranking with lucky small-N runs.
MIN_TRADES_ELIGIBLE = 5

# How many politicians to return
DEFAULT_TOP_N = 5

# Holding horizon (matches backtest convention)
HOLDING_HORIZON_DAYS = 60


# ---------------------------------------------------------------------------
# Ranking computation
# ---------------------------------------------------------------------------

def _candidate_politicians(conn, max_candidates: int = 50) -> List[str]:
    """
    Pull candidate politicians to rank. Limits to the top N most-active
    (by buy count) because evaluate_politician() is expensive — calling
    it on every politician in the DB would take hours.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT politician_name, COUNT(*) AS n
        FROM trades
        WHERE transaction_type = 'buy' AND trader_tag = 'member_direct'
        GROUP BY politician_name
        ORDER BY n DESC
        LIMIT ?
        """,
        (max_candidates,),
    )
    return [r["politician_name"] for r in cur.fetchall()]


def compute_top_performers(
    conn,
    n: int = DEFAULT_TOP_N,
    lookback_years: int = 5,
    max_candidates: int = 50,
) -> List[Dict]:
    """
    Rank politicians by recency-weighted excess return vs SPY.

    Uses backtest.evaluate_politician() on the top-N-most-active
    candidates (by buy count). Filters out anyone with fewer than
    MIN_TRADES_ELIGIBLE committee-aligned trades with usable price data.

    Returns list of dicts with:
        name, n_aligned, n_scored, hit_rate, avg_excess_pct,
        rec_hit_rate, rec_avg_excess_pct, wins, rank
    """
    candidates = _candidate_politicians(conn, max_candidates=max_candidates)
    print(f"[smart_money] ranking {len(candidates)} candidate politicians...",
          file=sys.stderr)

    results = []
    for name in candidates:
        try:
            stats = backtest.evaluate_politician(
                conn, name, lookback_years, min_trades=MIN_TRADES_ELIGIBLE
            )
        except Exception as e:
            print(f"  [smart_money] evaluate({name}) failed: {e}", file=sys.stderr)
            continue
        if not stats or stats.get("skip"):
            continue
        n_scored = stats.get("n_with_pricedata", 0)
        if n_scored < MIN_TRADES_ELIGIBLE:
            continue
        results.append(stats)

    # Sort by recency-weighted excess return DESC
    results.sort(key=lambda s: -(s.get("rec_avg_excess_pct") or 0))

    top_n = results[:n]
    for i, stats in enumerate(top_n, 1):
        stats["rank"] = i
        stats["n_aligned"] = stats.pop("n_aligned_trades", 0)
        stats["n_scored"] = stats.pop("n_with_pricedata", 0)

    print(f"[smart_money] top {len(top_n)} of {len(results)} eligible",
          file=sys.stderr)
    return top_n


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

def _load_cache() -> Optional[Dict]:
    """Load cached ranking. Returns None if missing, expired, or corrupted."""
    if not CACHE_PATH.exists():
        return None
    try:
        with open(CACHE_PATH) as f:
            cached = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # TTL check
    ts = cached.get("computed_at")
    if not ts:
        return None
    try:
        computed_dt = datetime.fromisoformat(ts.replace("Z", "+00:00").replace("+00:00", ""))
    except ValueError:
        return None
    age_days = (datetime.utcnow() - computed_dt).days
    if age_days > CACHE_TTL_DAYS:
        print(f"[smart_money] cache stale ({age_days}d old, TTL={CACHE_TTL_DAYS}d)",
              file=sys.stderr)
        return None

    return cached


def _save_cache(top_performers: List[Dict]) -> None:
    """Persist the ranking to disk with a timestamp."""
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "computed_at": datetime.utcnow().isoformat() + "Z",
        "top_performers": top_performers,
    }
    with open(CACHE_PATH, "w") as f:
        json.dump(payload, f, indent=2, default=str)


def get_top_performers(
    conn,
    n: int = DEFAULT_TOP_N,
    force_refresh: bool = False,
) -> List[Dict]:
    """
    Get the top-N ranking. Loads from cache if fresh, else recomputes.
    """
    if not force_refresh:
        cached = _load_cache()
        if cached:
            perfs = cached.get("top_performers", [])[:n]
            if perfs:
                return perfs

    # Recompute
    top = compute_top_performers(conn, n=n)
    _save_cache(top)
    return top


# ---------------------------------------------------------------------------
# Weekly roster update (Phase 2.4)
# ---------------------------------------------------------------------------
#
# The Weekly Deep Research agent re-ranks the Top 5 every Sunday night.
# If a non-Top-5 candidate has rec_avg_excess_pct at least MIN_CHALLENGE_MULT
# times the weakest current Top 5 member's excess (default 1.20 = 20% better),
# auto-apply the swap. Each change is logged to parameter_changelog for
# auditability.

# "Significantly better" threshold: new candidate must exceed incumbent's
# recency-weighted excess by at least this multiplier to trigger a swap.
MIN_CHALLENGE_MULT = 1.20


def update_roster_if_needed(
    conn,
    challenge_mult: float = MIN_CHALLENGE_MULT,
    dry_run: bool = False,
) -> Dict:
    """
    Re-rank Top 5 Congressional Traders, compare to current cached roster,
    and auto-apply changes if a challenger outperforms the weakest
    incumbent by >= challenge_mult × their rec_avg_excess_pct.

    Writes each change to parameter_changelog for audit. If any changes
    are applied, also overwrites data/top_performers.json with the new
    ranking so the next Daily Signal picks it up.

    Returns dict:
        {
          "prior_top5": [{name, rec_avg_excess_pct, rec_hit_rate, rank}, ...],
          "new_top5":   [...],
          "swaps":      [{out_name, in_name, out_excess, in_excess, threshold_met}, ...],
          "applied":    bool,
          "threshold":  float,
        }
    """
    # Load the CURRENT cached roster BEFORE recomputing
    prior = _load_cache() or {}
    prior_top5 = prior.get("top_performers") or []

    # Force-refresh the ranking
    new_top5 = compute_top_performers(conn, n=DEFAULT_TOP_N)

    # Identify swaps: walk old vs new by name
    prior_names = [p.get("name") for p in prior_top5]
    new_names = [p.get("name") for p in new_top5]

    # Build lookup of excess per politician for comparison
    prior_excess = {p.get("name"): (p.get("rec_avg_excess_pct") or 0) for p in prior_top5}
    new_excess = {p.get("name"): (p.get("rec_avg_excess_pct") or 0) for p in new_top5}

    swaps: List[Dict] = []

    for name in new_names:
        if name in prior_names:
            continue
        # This politician is NEW in the top 5. Who did they displace?
        displaced_candidates = [n for n in prior_names if n not in new_names]
        if not displaced_candidates:
            continue
        # Pair in rank order: the first displaced gets the first promoted
        # (both lists are rank-sorted). But let's be simpler and pair the
        # displaced politician with the LOWEST prior excess to the
        # promoted politician with the HIGHEST new excess.
        out_name = displaced_candidates[0]  # conservative: just pick the first displacement
        in_excess_val = new_excess.get(name) or 0
        out_excess_val = prior_excess.get(out_name) or 0

        # Threshold check: promoted must exceed displaced by challenge_mult
        threshold_met = False
        if out_excess_val == 0:
            # Incumbent was 0% excess → any positive new candidate is a clear win
            threshold_met = in_excess_val > 0.5  # must at least beat noise floor
        else:
            threshold_met = in_excess_val >= abs(out_excess_val) * challenge_mult

        swaps.append({
            "out_name": out_name,
            "in_name": name,
            "out_excess_pct": out_excess_val,
            "in_excess_pct": in_excess_val,
            "threshold_mult": challenge_mult,
            "threshold_met": threshold_met,
        })

    # Apply if at least one swap met threshold
    applied = False
    if swaps and any(s["threshold_met"] for s in swaps) and not dry_run:
        _save_cache(new_top5)
        # Log each change to parameter_changelog
        try:
            cur = conn.cursor()
            for s in swaps:
                if not s["threshold_met"]:
                    continue
                cur.execute(
                    """
                    INSERT INTO parameter_changelog (
                        timestamp, constant_name, old_value, new_value,
                        rationale, approval_status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.utcnow().isoformat(),
                        f"top5_roster:{s['out_name']}->{s['in_name']}",
                        s["out_excess_pct"],
                        s["in_excess_pct"],
                        (
                            f"Roster swap: {s['out_name']} (rec_excess "
                            f"{s['out_excess_pct']:+.2f}%) replaced by "
                            f"{s['in_name']} (rec_excess "
                            f"{s['in_excess_pct']:+.2f}%) — "
                            f"challenger exceeded incumbent by "
                            f">{challenge_mult:.0%} threshold."
                        ),
                        "auto-applied",
                    ),
                )
            conn.commit()
            applied = True
        except Exception as e:
            print(f"  [smart_money] failed to log roster change: {e}",
                  file=sys.stderr)

    return {
        "prior_top5": prior_top5,
        "new_top5": new_top5,
        "swaps": swaps,
        "applied": applied,
        "threshold_mult": challenge_mult,
    }


def render_roster_update_section(result: Dict) -> str:
    """
    Render a markdown block describing the roster update result. Called by
    the Weekly Deep Research agent's Phase A driver to append to the
    research pack.
    """
    lines: List[str] = []
    lines.append("## Roster Update — Top 5 Congressional Traders\n")

    swaps = result.get("swaps") or []
    applied = result.get("applied", False)
    threshold = result.get("threshold_mult", MIN_CHALLENGE_MULT)

    if not swaps:
        lines.append(
            "*No roster changes this week — the Top 5 roster is still current.*\n"
        )
        lines.append("Current Top 5:")
        for i, p in enumerate(result.get("new_top5", []), 1):
            excess = p.get("rec_avg_excess_pct")
            hit = p.get("rec_hit_rate")
            lines.append(
                f"{i}. **{p.get('name')}** — "
                f"{hit * 100:.0f}% hit / {excess:+.1f}% rec-wtd excess"
                if hit is not None and excess is not None
                else f"{i}. **{p.get('name')}**"
            )
        lines.append("")
        return "\n".join(lines) + "\n"

    # Some swaps detected — describe each
    applied_count = sum(1 for s in swaps if s["threshold_met"])
    nearmiss_count = sum(1 for s in swaps if not s["threshold_met"])

    if applied:
        lines.append(
            f"**{applied_count} roster change(s) auto-applied** "
            f"(threshold: challenger must beat incumbent by "
            f"{(threshold - 1) * 100:.0f}%). "
            f"Changes logged to `parameter_changelog`.\n"
        )
    else:
        lines.append(
            f"*{nearmiss_count} near-miss roster challenge(s) this week — "
            f"none met the {(threshold - 1) * 100:.0f}% threshold for "
            f"auto-promotion.*\n"
        )

    lines.append("### Changes evaluated\n")
    lines.append("| Status | Out | Out excess | In | In excess | Δ |")
    lines.append("|---|---|---|---|---|---|")
    for s in swaps:
        status = "APPLIED" if s["threshold_met"] else "near-miss"
        delta = s["in_excess_pct"] - s["out_excess_pct"]
        lines.append(
            f"| {status} "
            f"| {s['out_name']} "
            f"| {s['out_excess_pct']:+.1f}% "
            f"| {s['in_name']} "
            f"| {s['in_excess_pct']:+.1f}% "
            f"| {delta:+.1f} pts |"
        )
    lines.append("")

    lines.append("### New Top 5 (effective immediately)\n")
    for i, p in enumerate(result.get("new_top5", []), 1):
        excess = p.get("rec_avg_excess_pct")
        hit = p.get("rec_hit_rate")
        marker = ""
        if p.get("name") not in [x.get("name") for x in result.get("prior_top5", [])]:
            marker = " **(new)**"
        lines.append(
            f"{i}. **{p.get('name')}**{marker} — "
            f"{hit * 100:.0f}% hit / {excess:+.1f}% rec-wtd excess"
            if hit is not None and excess is not None
            else f"{i}. **{p.get('name')}**{marker}"
        )
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Open-position tracking
# ---------------------------------------------------------------------------

def get_open_positions(
    conn,
    politician_name: str,
    horizon_days: int = HOLDING_HORIZON_DAYS,
    today: Optional[str] = None,
) -> List[Dict]:
    """
    Return trades by this politician where trade_date + horizon_days is
    still in the future (i.e., the 60-day holding window is still open).

    Filters to buys only (sells don't make sense as "open positions"
    to track for P&L) and member_direct only.

    For each open trade, computes current price move vs entry and
    excess return vs SPY over the elapsed window.

    Returns list of dicts with:
        ticker, trade_date, amount_range, days_elapsed, days_remaining,
        entry_price, current_price, pct_move, spy_pct_move, excess_pct
    """
    if today is None:
        today = datetime.utcnow().strftime("%Y-%m-%d")

    today_dt = datetime.strptime(today, "%Y-%m-%d").date()

    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, ticker, trade_date, disclosure_date, amount_range,
               transaction_type
        FROM trades
        WHERE politician_name = ?
          AND transaction_type = 'buy'
          AND trader_tag = 'member_direct'
          AND DATE(trade_date) >= DATE(?, ?)
          AND DATE(trade_date) <= DATE(?)
        ORDER BY trade_date DESC
        """,
        (politician_name, today, f"-{horizon_days} days", today),
    )
    rows = cur.fetchall()

    out: List[Dict] = []
    for r in rows:
        trade_date = r["trade_date"]
        try:
            td_dt = datetime.strptime(trade_date, "%Y-%m-%d").date()
        except ValueError:
            continue

        days_elapsed = (today_dt - td_dt).days
        days_remaining = horizon_days - days_elapsed
        if days_remaining <= 0:
            continue  # defensive; SQL should have filtered this

        ticker = r["ticker"]
        move = stock_metrics.compute_actual_move(ticker, trade_date)
        if not move:
            continue  # no price data

        spy_move = stock_metrics.compute_actual_move("SPY", trade_date)
        spy_pct = spy_move["signed_pct_move"] if spy_move else None
        pct = move["signed_pct_move"]
        excess = pct - spy_pct if spy_pct is not None else None

        out.append({
            "trade_id": r["id"],
            "ticker": ticker,
            "trade_date": trade_date,
            "disclosure_date": r["disclosure_date"],
            "amount_range": r["amount_range"],
            "days_elapsed": days_elapsed,
            "days_remaining": days_remaining,
            "entry_price": move["entry_price"],
            "current_price": move["exit_price"],
            "pct_move": pct,
            "spy_pct_move": spy_pct,
            "excess_pct": excess,
        })

    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_pct(v: Optional[float], signed: bool = True, digits: int = 1) -> str:
    if v is None:
        return "—"
    pct = v * 100
    if signed:
        # Use the built-in + formatter so negatives stay single-signed
        return f"{pct:+.{digits}f}%"
    return f"{pct:.{digits}f}%"


def _fmt_pct_already_pct(v: Optional[float], digits: int = 1) -> str:
    """For values already in pct (e.g., avg_excess_pct = 9.8 means 9.8%)."""
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def _fmt_hit_rate(hr: Optional[float]) -> str:
    if hr is None:
        return "—"
    return f"{hr * 100:.0f}%"


def _summarize_open_positions(positions: List[Dict]) -> Dict:
    """Produce summary stats from a list of open positions."""
    if not positions:
        return {
            "n": 0,
            "best_ticker": None,
            "best_excess": None,
            "worst_ticker": None,
            "worst_excess": None,
        }
    scored = [p for p in positions if p.get("excess_pct") is not None]
    if not scored:
        return {
            "n": len(positions),
            "best_ticker": None,
            "best_excess": None,
            "worst_ticker": None,
            "worst_excess": None,
        }
    scored.sort(key=lambda p: -p["excess_pct"])
    best = scored[0]
    worst = scored[-1]
    return {
        "n": len(positions),
        "best_ticker": best["ticker"],
        "best_excess": best["excess_pct"],
        "worst_ticker": worst["ticker"],
        "worst_excess": worst["excess_pct"],
    }


def render_smart_money_section(
    conn,
    n: int = DEFAULT_TOP_N,
    today: Optional[str] = None,
    force_refresh: bool = False,
    chart_registry=None,
) -> str:
    """
    Render the Smart Money Watchlist as a markdown section.

    Structure:
      ## Smart Money Watchlist — Top N Historical Performers
      (brief intro)
      (table with 5 rows, one per politician)
      (optional per-politician open-position bullets if any have activity)
    """
    top = get_top_performers(conn, n=n, force_refresh=force_refresh)

    lines: List[str] = []
    lines.append(f"## Smart Money Watchlist — Top {n} Historical Performers\n")
    lines.append(
        "*Ranked by recency-weighted excess return vs SPY on committee-"
        "aligned trades (last 5 years). Table shows their currently-"
        f"open positions — trades within the {HOLDING_HORIZON_DAYS}-day "
        "holding window where the thesis is still live.*\n"
    )

    if not top:
        lines.append(
            "*Insufficient data — no politicians currently meet the "
            f"{MIN_TRADES_ELIGIBLE}-aligned-trade sample gate. Run "
            "`./run-agent.sh backtest` to refresh the roster.*\n"
        )
        return "\n".join(lines) + "\n"

    # ---- Compact table ----
    lines.append("| # | Politician | Track Record | Open Trades | Best Open | Worst Open |")
    lines.append("|---|---|---|---|---|---|")

    politician_position_details: List[Dict] = []  # for the appendix bullets

    for perf in top:
        name = perf.get("name", "—")
        rank = perf.get("rank", "?")
        n_scored = perf.get("n_scored", perf.get("n_with_pricedata", 0))
        rec_hit = perf.get("rec_hit_rate")
        rec_excess = perf.get("rec_avg_excess_pct")

        track_record = (
            f"{_fmt_hit_rate(rec_hit)} / {_fmt_pct_already_pct(rec_excess)} "
            f"(N={n_scored})"
        )

        positions = get_open_positions(conn, name, today=today)
        summary = _summarize_open_positions(positions)

        open_trades_cell = f"{summary['n']}"
        best_cell = "—"
        worst_cell = "—"
        if summary["best_ticker"] is not None:
            best_cell = (
                f"{summary['best_ticker']} "
                f"{_fmt_pct(summary['best_excess'])}"
            )
        if summary["worst_ticker"] is not None and summary["worst_ticker"] != summary["best_ticker"]:
            worst_cell = (
                f"{summary['worst_ticker']} "
                f"{_fmt_pct(summary['worst_excess'])}"
            )

        lines.append(
            f"| {rank} | **{name}** | {track_record} | {open_trades_cell} "
            f"| {best_cell} | {worst_cell} |"
        )

        politician_position_details.append({
            "name": name,
            "positions": positions,
        })

    lines.append("")

    # ---- Per-politician open-position detail (only if any have positions) ----
    any_positions = any(
        detail["positions"] for detail in politician_position_details
    )
    if any_positions:
        lines.append("### Open positions detail\n")
        for detail in politician_position_details:
            if not detail["positions"]:
                continue
            lines.append(f"**{detail['name']}**\n")
            # Sort positions by disclosure_date desc so newest on top
            sorted_positions = sorted(
                detail["positions"],
                key=lambda p: p.get("disclosure_date") or p.get("trade_date") or "",
                reverse=True,
            )
            for p in sorted_positions[:5]:  # cap at 5 most-recent per politician
                excess_str = _fmt_pct(p.get("excess_pct"))
                stock_str = _fmt_pct(p.get("pct_move"))
                line = (
                    f"- {p['ticker']} bought {p['trade_date']} "
                    f"({p.get('amount_range', '—')}) — "
                    f"${p['entry_price']:.2f} → ${p['current_price']:.2f} "
                    f"({stock_str} stock, {excess_str} vs SPY, "
                    f"{p['days_remaining']}d remaining)"
                )
                lines.append(line)
                # Register a sparkline (placeholder token in pack; actual
                # base64 lives in the sidecar until format_report.py
                # substitutes it).
                if chart_registry is not None:
                    try:
                        import charts as _charts
                        placeholder = _charts.register_sparkline(
                            chart_registry, p["ticker"], p["trade_date"]
                        )
                        if placeholder:
                            lines.append(f"  {placeholder}")
                    except Exception:
                        pass  # silently skip; bullet list still renders
            lines.append("")
    else:
        lines.append(
            "*None of the top performers have open positions in the "
            f"trailing {HOLDING_HORIZON_DAYS}-day window. "
            "Quiet month for the smart money.*\n"
        )

    return "\n".join(lines) + "\n"


# Convenience alias that matches the import pattern used in daily_signal.py
def smart_money_section(conn, **kwargs) -> str:
    return render_smart_money_section(conn, **kwargs)


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json

    cmd = sys.argv[1] if len(sys.argv) >= 2 else "show"
    conn = db.connect()

    if cmd == "refresh":
        print("Recomputing top performers (force refresh)...")
        top = compute_top_performers(conn)
        _save_cache(top)
        print(f"\nTop {len(top)} saved to {CACHE_PATH}:")
        for p in top:
            print(f"  #{p['rank']} {p['name']:30s} "
                  f"rec_excess={_fmt_pct_already_pct(p.get('rec_avg_excess_pct'))} "
                  f"rec_hit={_fmt_hit_rate(p.get('rec_hit_rate'))} "
                  f"N={p.get('n_scored', 0)}")

    elif cmd == "show":
        section = render_smart_money_section(conn)
        print(section)

    elif cmd == "ranking":
        top = get_top_performers(conn)
        print(_json.dumps(top, indent=2, default=str))

    elif cmd == "positions":
        name = sys.argv[2] if len(sys.argv) >= 3 else "Mark Green"
        positions = get_open_positions(conn, name)
        print(f"Open positions for {name}: {len(positions)}")
        for p in positions:
            print(_json.dumps(p, indent=2, default=str))

    else:
        print("Usage: python3 smart_money.py [refresh|show|ranking|positions NAME]")

    backtest.save_price_cache()
    conn.close()
