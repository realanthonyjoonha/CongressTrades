#!/usr/bin/env python3
"""
weekly_deep.py — Phase A driver for the Weekly Deep Research agent
(Phase 2.4, Agent 5 in the brainstorm numbering).

Runs Sunday nights. Pulls trades flagged by the Daily Signal agent
during the past 7 days, computes a price-only retrospective per trade,
re-scores each via pipeline.score_trade to catch any state changes,
aggregates tier/sector/politician rollups, selects the top 10 trades
for deep LLM research, and writes a research pack markdown file for
Phase B (the LLM narrative + deep-dive web research).

Phase B reads the pack via prompts/weekly_deep.md, does 15 web searches
per selected trade (150 max budget), and emits a weekly narrative
ending with a fenced JSON block of per-trade follow-up findings that
the runner round-trips back to the DB.

What's NOT in this agent (v1):
  - Paper trading P&L (dropped permanently during brainstorm revision)
  - Real options notional via MMD (Phase 2.3.5 dependency)
  - Feedback loop / parameter recalibration (Phase 3)
  - Automated roster demotion (surface observations only)

CLI:
    python3 scripts/weekly_deep.py                                 # full auto
    python3 scripts/weekly_deep.py --lookback 14                   # 2-wk window
    python3 scripts/weekly_deep.py --limit-trades 5                # tighter LLM input
    python3 scripts/weekly_deep.py --dry-run                       # no DB writes
    python3 scripts/weekly_deep.py --out outputs/tmp/weekly.md     # custom path
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# Local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import backtest  # noqa: E402
import pipeline  # noqa: E402
import stock_metrics  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
TMP_DIR = BASE_DIR / "outputs" / "tmp"
LOG_DIR = BASE_DIR / "outputs" / "logs"

DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_LLM_TRADE_CAP = 10  # tighter than Daily's 20 because research per trade is deeper


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_weekly_flagged(conn, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> List[Dict]:
    """Pull all trades flagged by Daily Signal in the trailing window."""
    rows = db.get_weekly_flagged_trades(conn, lookback_days=lookback_days)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Price-only retrospective
# ---------------------------------------------------------------------------

def compute_retrospective(trade: Dict) -> Dict:
    """
    Price-only retrospective for one flagged trade.

    Computes:
      - entry_price    (ticker's close on trade_date)
      - current_price  (ticker's latest available close)
      - days_held      (trade_date to today)
      - stock_return   (% move of ticker since trade)
      - spy_return     (% move of SPY over same window)
      - excess_return  (stock - SPY)
      - spy_benchmark  (bool — did ticker beat SPY?)

    Returns a dict; any field may be None if yfinance data is missing.
    No options math — conceptual plays from Daily Signal don't have
    strikes, so there's nothing to mark-to-market.
    """
    ticker = trade.get("ticker")
    trade_date = trade.get("trade_date")
    if not ticker or not trade_date:
        return {"error": "missing ticker or trade_date"}

    move = stock_metrics.compute_actual_move(ticker, trade_date)
    if not move:
        return {"error": "no price data"}

    # SPY benchmark for the same window
    spy_move = stock_metrics.compute_actual_move(
        "SPY", trade_date, end_date=move.get("exit_date")
    )
    spy_return = spy_move["signed_pct_move"] if spy_move else None

    stock_return = move["signed_pct_move"]
    excess = None if spy_return is None else (stock_return - spy_return)

    return {
        "entry_price": move["entry_price"],
        "current_price": move["exit_price"],
        "entry_date": move["entry_date"],
        "current_date": move["exit_date"],
        "days_held": move["days_elapsed"],
        "stock_return_pct": stock_return,
        "spy_return_pct": spy_return,
        "excess_return_pct": excess,
        "spy_benchmark": None if excess is None else excess > 0,
    }


# ---------------------------------------------------------------------------
# Re-scoring (in case anything changed since Daily Signal)
# ---------------------------------------------------------------------------

def rescore_trade(conn, trade: Dict) -> Dict:
    """
    Re-run pipeline.score_trade on a flagged trade to pick up any state
    changes since the original Daily Signal run (e.g., new clustering
    from politicians who filed after the original scoring, or OWD
    verdict changes as more time has passed and the stock has moved).

    Returns dict with {changed, prior_tier, new_tier, stage1, stage2, stage4}.
    """
    try:
        result = pipeline.score_trade(conn, trade)
    except Exception as e:
        return {"error": f"rescore failed: {e}"}

    prior_tier = trade.get("final_signal_tier")
    new_tier = result.get("tier_pre_stage3")
    return {
        "changed": prior_tier != new_tier,
        "prior_tier": prior_tier,
        "new_tier": new_tier,
        "stage1": result.get("stage1", {}),
        "stage2": result.get("stage2", {}),
        "stage4": result.get("stage4", {}),
    }


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------

def aggregate_week(flagged: List[Dict], retro_map: Dict[int, Dict]) -> Dict:
    """
    Compute weekly rollups:
      - tier_counts           : {STRONG, BASE, MODERATE, SKIP} counts
      - by_politician         : {politician_name: {count, tiers, top_tier}}
      - by_sector             : {sector: count}
      - cluster_hot_tickers   : tickers with 3+ politicians this week
      - cross_party_tickers   : tickers with politicians from both parties
      - top_winners           : trades with the highest excess return
      - top_losers            : trades with the lowest excess return
      - benchmark_hit_rate    : fraction of flagged trades that beat SPY
    """
    tier_counts: Counter = Counter()
    by_politician: Dict[str, Dict] = defaultdict(lambda: {"count": 0, "tiers": Counter()})
    by_sector: Counter = Counter()
    ticker_politicians: Dict[str, set] = defaultdict(set)
    ticker_parties: Dict[str, set] = defaultdict(set)

    excess_list: List[Dict] = []
    benchmark_hits = 0
    benchmark_total = 0

    for trade in flagged:
        tier = trade.get("final_signal_tier") or "SKIP"
        tier_counts[tier] += 1

        pol = trade.get("politician_name") or "unknown"
        by_politician[pol]["count"] += 1
        by_politician[pol]["tiers"][tier] += 1

        sector = trade.get("sector") or "unclassified"
        by_sector[sector] += 1

        ticker = trade.get("ticker")
        if ticker:
            ticker_politicians[ticker].add(pol)
            # Party from politicians table would require a JOIN; skip for v1
            # and use trader_tag as a proxy for later investigation
            ticker_parties[ticker].add(trade.get("trader_tag") or "unknown")

        retro = retro_map.get(trade.get("id"), {})
        excess = retro.get("excess_return_pct")
        if excess is not None:
            excess_list.append({
                "trade_id": trade.get("id"),
                "politician": pol,
                "ticker": ticker,
                "tier": tier,
                "excess": excess,
                "stock_return": retro.get("stock_return_pct"),
                "days_held": retro.get("days_held"),
            })
            benchmark_total += 1
            if retro.get("spy_benchmark"):
                benchmark_hits += 1

    # Cluster hot: tickers with 3+ flaggings this week
    cluster_hot = [
        (ticker, len(pols))
        for ticker, pols in ticker_politicians.items()
        if len(pols) >= 3
    ]
    cluster_hot.sort(key=lambda x: -x[1])

    # Top politicians by count
    top_politicians = sorted(
        by_politician.items(),
        key=lambda x: (-x[1]["count"], x[0]),
    )

    # Sort by excess
    excess_list.sort(key=lambda e: -e["excess"])
    top_winners = excess_list[:5]
    top_losers = list(reversed(excess_list[-5:])) if len(excess_list) >= 5 else []

    benchmark_hit_rate = None
    if benchmark_total > 0:
        benchmark_hit_rate = benchmark_hits / benchmark_total

    return {
        "tier_counts": dict(tier_counts),
        "by_politician": [
            {"name": name, "count": info["count"], "tiers": dict(info["tiers"])}
            for name, info in top_politicians
        ],
        "by_sector": dict(by_sector.most_common()),
        "cluster_hot_tickers": cluster_hot,
        "top_winners": top_winners,
        "top_losers": top_losers,
        "benchmark_hit_rate": benchmark_hit_rate,
        "benchmark_total": benchmark_total,
        "benchmark_hits": benchmark_hits,
    }


# ---------------------------------------------------------------------------
# LLM input selection
# ---------------------------------------------------------------------------

def select_for_llm(
    flagged: List[Dict],
    rescore_map: Dict[int, Dict],
    retro_map: Dict[int, Dict],
    max_trades: int = DEFAULT_LLM_TRADE_CAP,
) -> List[Dict]:
    """
    Pick the top-N trades for deep LLM research. Selection criteria:

      1. STRONG tier first (hard cap)
      2. Then BASE tier, sorted by clustering count desc, then excess return
      3. Then MODERATE (only if we still have slots)
      4. Drop trades where rescore result flipped to SKIP

    Deep research is more expensive per trade than the Daily Signal version
    (15 searches/trade vs 5-10), so the cap is tighter (10 vs 20).
    """
    tier_priority = {"STRONG": 0, "BASE": 1, "MODERATE": 2, "SKIP": 99}
    eligible = []
    for trade in flagged:
        rescore = rescore_map.get(trade.get("id"), {})
        if rescore.get("new_tier") == "SKIP":
            continue  # rescore killed it
        tier = trade.get("final_signal_tier") or "SKIP"
        if tier == "SKIP":
            continue
        retro = retro_map.get(trade.get("id"), {})
        eligible.append({
            "trade": trade,
            "priority": tier_priority.get(tier, 99),
            "cluster_count": trade.get("clustering_count") or 0,
            "excess_return": retro.get("excess_return_pct") or 0,
            "rescore": rescore,
            "retro": retro,
        })

    eligible.sort(
        key=lambda e: (
            e["priority"],
            -int(e["cluster_count"] or 0),
            -float(e["excess_return"] or 0),
        )
    )
    return eligible[:max_trades]


# ---------------------------------------------------------------------------
# Research pack rendering
# ---------------------------------------------------------------------------

def _fmt_pct(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.{digits}f}%"


def _fmt_num(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def render_weekly_pack(
    flagged: List[Dict],
    rescore_map: Dict[int, Dict],
    retro_map: Dict[int, Dict],
    aggregates: Dict,
    selected: List[Dict],
    lookback_days: int,
    today: str,
) -> str:
    """
    Assemble the markdown research pack the LLM Phase B reads.

    Contents:
      1. Run metadata
      2. Weekly tier / politician / sector rollups
      3. Price-only retrospective (top winners/losers, benchmark hit rate)
      4. Cluster hot list
      5. Re-score diffs (trades whose tier changed since Daily Signal)
      6. Deep-dive selection (for LLM research)
      7. Full compact table of all flagged trades
    """
    lines: List[str] = []
    lines.append(f"# Weekly Deep Research — Research Pack ({today})\n")
    lines.append(
        f"*Lookback window: {lookback_days} days. "
        f"Total flagged trades: {len(flagged)}.*\n"
    )

    # ---- 1. Summary ----
    tier_counts = aggregates.get("tier_counts", {})
    lines.append("## Weekly summary\n")
    lines.append(f"- **STRONG:** {tier_counts.get('STRONG', 0)}")
    lines.append(f"- **BASE:** {tier_counts.get('BASE', 0)}")
    lines.append(f"- **MODERATE:** {tier_counts.get('MODERATE', 0)}")
    lines.append(f"- **SKIP:** {tier_counts.get('SKIP', 0)}")
    lines.append(f"- **Total:** {len(flagged)}")
    bhr = aggregates.get("benchmark_hit_rate")
    if bhr is not None:
        lines.append(
            f"- **vs-SPY benchmark:** "
            f"{bhr * 100:.1f}% beat SPY ({aggregates.get('benchmark_hits')}"
            f"/{aggregates.get('benchmark_total')})"
        )
    lines.append("")

    if not flagged:
        lines.append("## No flagged trades this week\n")
        lines.append(
            "The Daily Signal agent did not flag any trades in the trailing "
            f"{lookback_days}-day window. This may mean no new disclosures "
            "were processed, the pipeline scored all new trades below the "
            "actionable threshold, or the Data Maintenance agent was not "
            "run this week.\n"
        )
        return "\n".join(lines) + "\n"

    # ---- 2. Politicians ----
    lines.append("## By politician\n")
    lines.append("| Rank | Politician | Total | Tiers |")
    lines.append("|---|---|---|---|")
    for i, p in enumerate(aggregates.get("by_politician", [])[:15], 1):
        tier_summary = ", ".join(
            f"{t}:{n}" for t, n in p["tiers"].items()
        )
        lines.append(f"| {i} | {p['name'][:30]} | {p['count']} | {tier_summary} |")
    lines.append("")

    # ---- 3. Sectors ----
    lines.append("## By sector\n")
    lines.append("| Rank | Sector | Count |")
    lines.append("|---|---|---|")
    for i, (sector, n) in enumerate(list(aggregates.get("by_sector", {}).items())[:10], 1):
        lines.append(f"| {i} | {sector} | {n} |")
    lines.append("")

    # ---- 4. Cluster hot list ----
    cluster_hot = aggregates.get("cluster_hot_tickers", [])
    if cluster_hot:
        lines.append("## Cluster hot list (3+ politicians on same ticker this week)\n")
        lines.append("| Ticker | N politicians |")
        lines.append("|---|---|")
        for ticker, n in cluster_hot[:15]:
            lines.append(f"| {ticker} | {n} |")
        lines.append("")

    # ---- 5. Retrospective: top winners / losers ----
    lines.append("## Price-only retrospective (vs SPY benchmark)\n")
    lines.append(
        "*For each flagged trade, we compare the stock's move since "
        "trade_date to SPY's move over the same window. Informational "
        "only — no options math, no paper P&L, no position sizing.*\n"
    )

    top_winners = aggregates.get("top_winners", [])
    if top_winners:
        lines.append("### Top 5 winners (excess vs SPY)\n")
        lines.append("| # | Politician | Ticker | Tier | Days | Stock | vs SPY |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, w in enumerate(top_winners, 1):
            lines.append(
                f"| {i} | {(w['politician'] or '—')[:25]} | {w['ticker'] or '—'} "
                f"| {w['tier']} | {w['days_held'] or '—'} "
                f"| {_fmt_pct(w['stock_return'])} | {_fmt_pct(w['excess'])} |"
            )
        lines.append("")

    top_losers = aggregates.get("top_losers", [])
    if top_losers:
        lines.append("### Top 5 losers (excess vs SPY)\n")
        lines.append("| # | Politician | Ticker | Tier | Days | Stock | vs SPY |")
        lines.append("|---|---|---|---|---|---|---|")
        for i, l in enumerate(top_losers, 1):
            lines.append(
                f"| {i} | {(l['politician'] or '—')[:25]} | {l['ticker'] or '—'} "
                f"| {l['tier']} | {l['days_held'] or '—'} "
                f"| {_fmt_pct(l['stock_return'])} | {_fmt_pct(l['excess'])} |"
            )
        lines.append("")

    # ---- 6. Rescore diffs ----
    diffs = [
        (tid, r) for tid, r in rescore_map.items()
        if r.get("changed") and not r.get("error")
    ]
    if diffs:
        lines.append(f"## Rescore diffs ({len(diffs)} trades changed tier since Daily Signal)\n")
        lines.append("| Trade ID | Politician | Ticker | Prior | New | Reason |")
        lines.append("|---|---|---|---|---|---|")
        trades_by_id = {t["id"]: t for t in flagged}
        for tid, r in diffs[:20]:
            t = trades_by_id.get(tid, {})
            reason_parts = []
            s4 = r.get("stage4", {})
            if s4.get("cluster_count") != t.get("clustering_count"):
                reason_parts.append(f"cluster {t.get('clustering_count')}→{s4.get('cluster_count')}")
            s2 = r.get("stage2", {})
            if s2.get("verdict") != t.get("owd_verdict"):
                reason_parts.append(f"OWD {t.get('owd_verdict')}→{s2.get('verdict')}")
            reason = ", ".join(reason_parts) or "unclear"
            lines.append(
                f"| {tid} | {(t.get('politician_name') or '—')[:20]} "
                f"| {t.get('ticker') or '—'} | {r.get('prior_tier')} "
                f"| {r.get('new_tier')} | {reason} |"
            )
        lines.append("")

    # ---- 7. Deep-dive selection for LLM ----
    lines.append(f"## Deep-dive selection: {len(selected)} trades\n")
    lines.append(
        "**Web search budget: 15 searches per trade, 150 max total.**\n"
    )
    lines.append(
        "For each trade below, do deeper research than the Daily Signal "
        "agent: (a) all upcoming committee work in the politician's "
        "jurisdiction over the next 90 days; (b) sector-wide catalysts "
        "that could affect the ticker; (c) any analyst upgrades / "
        "downgrades / price target changes since trade_date; (d) executive "
        "team changes, M&A rumors, or other structural factors; (e) "
        "validate or falsify the thesis from Daily Signal.\n"
    )

    for i, sel in enumerate(selected, 1):
        t = sel["trade"]
        r = sel["rescore"]
        retro = sel["retro"]
        tier = t.get("final_signal_tier")

        lines.append(f"### Deep-dive {i}: {t.get('politician_name')} — {t.get('ticker')} ({tier})\n")
        lines.append(f"- **trade_id:** {t.get('id')}")
        lines.append(f"- **trade_date:** {t.get('trade_date')}")
        lines.append(f"- **disclosure_date:** {t.get('disclosure_date')}")
        lines.append(f"- **amount_range:** {t.get('amount_range') or '—'}")
        lines.append(f"- **sector:** {t.get('sector') or '—'}")
        lines.append(f"- **trader_tag:** {t.get('trader_tag')}")

        lines.append(f"\n**Daily Signal output:**")
        lines.append(f"- alignment_multiplier: {t.get('alignment_multiplier')}")
        lines.append(f"- OWD: {t.get('owd_verdict')}({t.get('owd_total')})")
        lines.append(f"- clustering: {t.get('clustering_count')} "
                     f"(cross_party={t.get('cross_party_cluster')})")
        if t.get("forward_catalyst"):
            lines.append(f"- forward_catalyst (from Daily): {t.get('forward_catalyst')}")
        else:
            lines.append(f"- forward_catalyst: none recorded")

        lines.append(f"\n**Retrospective:**")
        if retro.get("error"):
            lines.append(f"- *(data unavailable: {retro['error']})*")
        else:
            lines.append(
                f"- entry ${retro.get('entry_price', 0):.2f} → "
                f"current ${retro.get('current_price', 0):.2f} "
                f"({_fmt_pct(retro.get('stock_return_pct'))} over "
                f"{retro.get('days_held')} days)"
            )
            if retro.get("excess_return_pct") is not None:
                lines.append(
                    f"- vs SPY: {_fmt_pct(retro.get('excess_return_pct'))} "
                    f"(SPY: {_fmt_pct(retro.get('spy_return_pct'))})"
                )
            if retro.get("spy_benchmark") is not None:
                verdict = "BEAT" if retro["spy_benchmark"] else "LOST TO"
                lines.append(f"- **{verdict} SPY benchmark**")

        if r.get("changed"):
            lines.append(f"\n**Rescore alert:** tier changed from "
                         f"`{r.get('prior_tier')}` to `{r.get('new_tier')}` since Daily Signal")
        lines.append("")

    # ---- 8. Full compact table ----
    lines.append("\n## All flagged trades (compact view)\n")
    lines.append("| # | Politician | Ticker | Tier | Trade Date | Cluster | Retro vs SPY |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, t in enumerate(flagged, 1):
        retro = retro_map.get(t.get("id"), {})
        excess_str = _fmt_pct(retro.get("excess_return_pct")) if "error" not in retro else "—"
        lines.append(
            f"| {i} | {(t.get('politician_name') or '—')[:25]} "
            f"| {t.get('ticker') or '—'} | {t.get('final_signal_tier') or '—'} "
            f"| {t.get('trade_date') or '—'} | {t.get('clustering_count') or 0} "
            f"| {excess_str} |"
        )
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Phase A driver for Weekly Deep Research agent")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help=f"Trailing window in days (default {DEFAULT_LOOKBACK_DAYS})")
    ap.add_argument("--limit-trades", type=int, default=DEFAULT_LLM_TRADE_CAP,
                    help=f"Max trades sent to LLM (default {DEFAULT_LLM_TRADE_CAP})")
    ap.add_argument("--out", type=str, default=None,
                    help="Output path for the research pack markdown")
    ap.add_argument("--dry-run", action="store_true",
                    help="No-op flag for now; weekly doesn't write to DB in Phase A")
    args = ap.parse_args()

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    print(f"[weekly-deep] start — {timestamp}", file=sys.stderr)

    conn = db.connect()

    # ---- Fetch flagged trades from the past week ----
    flagged = fetch_weekly_flagged(conn, lookback_days=args.lookback)
    print(f"[weekly-deep] found {len(flagged)} flagged trades "
          f"(lookback={args.lookback}d)", file=sys.stderr)

    # ---- Compute retrospective + rescore for each ----
    retro_map: Dict[int, Dict] = {}
    rescore_map: Dict[int, Dict] = {}
    for i, t in enumerate(flagged, 1):
        tid = t.get("id")
        if not tid:
            continue
        retro_map[tid] = compute_retrospective(t)
        rescore_map[tid] = rescore_trade(conn, t)
        if i % 10 == 0:
            print(f"  [phase-a] processed {i}/{len(flagged)}", file=sys.stderr)

    # ---- Aggregate weekly rollups ----
    aggregates = aggregate_week(flagged, retro_map)
    print(f"[weekly-deep] aggregates: "
          f"STRONG={aggregates['tier_counts'].get('STRONG', 0)}, "
          f"BASE={aggregates['tier_counts'].get('BASE', 0)}, "
          f"MODERATE={aggregates['tier_counts'].get('MODERATE', 0)}, "
          f"bench_hit_rate={aggregates.get('benchmark_hit_rate')}",
          file=sys.stderr)

    # ---- Select for LLM ----
    selected = select_for_llm(flagged, rescore_map, retro_map, max_trades=args.limit_trades)
    print(f"[weekly-deep] selected {len(selected)} for LLM deep research",
          file=sys.stderr)

    # ---- Render ----
    pack = render_weekly_pack(
        flagged, rescore_map, retro_map, aggregates, selected,
        args.lookback, today,
    )
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(pack)
        print(f"[weekly-deep] wrote research pack: {out_path} ({len(pack):,} bytes)",
              file=sys.stderr)
    else:
        sys.stdout.write(pack)

    backtest.save_price_cache()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
