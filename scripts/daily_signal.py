#!/usr/bin/env python3
"""
daily_signal.py — Phase A driver for the Daily Signal agent (Phase 2.3).

Pulls overnight disclosures, runs each one through Stages 1, 2 (Lite),
and 4 of the signal pipeline, persists Phase A diagnostics to the DB,
and writes a research pack markdown file for Phase B (the LLM narrative
+ Stage 3 forward-catalyst search).

Phase B reads the research pack via prompts/daily_signal.md and emits a
narrative report ending with a fenced JSON block containing Stage 3
results. The runner (run-agent.sh daily) parses that JSON, calls
assemble_final_tier() to compute the canonical tier, and persists the
final result.

CLI:
    python3 scripts/daily_signal.py                          # full auto
    python3 scripts/daily_signal.py --dry-run                # no DB writes
    python3 scripts/daily_signal.py --lookback 7             # custom window
    python3 scripts/daily_signal.py --limit-trades 20        # cap LLM input
    python3 scripts/daily_signal.py --out outputs/tmp/p.md   # custom output
    python3 scripts/daily_signal.py --no-persist             # research only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import backtest  # noqa: E402
import pipeline  # noqa: E402
import options_concept  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
TMP_DIR = BASE_DIR / "outputs" / "tmp"
LOG_DIR = BASE_DIR / "outputs" / "logs"

DEFAULT_LOOKBACK_DAYS = 3
DEFAULT_LLM_TRADE_CAP = 20  # Hard ceiling on trades sent to LLM Phase B


# ---------------------------------------------------------------------------
# Phase A: fetch + score overnight trades
# ---------------------------------------------------------------------------

def fetch_overnight(conn, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> List[Dict]:
    """
    Pull trades disclosed in the trailing window that have not yet been
    scored. Returns list of plain dicts (not sqlite3.Row).
    """
    rows = db.get_overnight_trades(conn, lookback_days=lookback_days)
    return [dict(r) for r in rows]


def score_all(conn, trades: List[Dict]) -> List[Dict]:
    """
    Run pipeline.score_trade() on each trade. Returns list of merged
    dicts with stage1/stage2/stage4 + tier_pre_stage3.
    """
    params = pipeline.load_params(conn)
    results: List[Dict] = []
    for i, trade in enumerate(trades, 1):
        try:
            scored = pipeline.score_trade(conn, trade, params=params)
        except Exception as e:
            print(f"  [score error] trade_id={trade.get('id')} ticker={trade.get('ticker')}: {e}",
                  file=sys.stderr)
            scored = {
                "trade_id": trade.get("id"),
                "tier_pre_stage3": "SKIP",
                "skip_reason": f"score error: {e}",
            }
        # Attach the original trade row for downstream rendering
        scored["_trade"] = trade
        results.append(scored)
        if i % 5 == 0:
            print(f"  [phase-a] scored {i}/{len(trades)}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# Phase A: persist diagnostics to DB
# ---------------------------------------------------------------------------

def persist_diagnostics(conn, scored: List[Dict], dry_run: bool = False) -> int:
    """
    Write each scored trade's Stage 1+2+4 results to trade_diagnostics
    and update the denormalized columns on trades.
    Idempotent on trade_id.
    Returns number of rows persisted.
    """
    if dry_run:
        return 0

    n = 0
    for s in scored:
        trade_id = s.get("trade_id")
        if not trade_id:
            continue
        if s.get("skip_reason") and not s.get("stage1"):
            # Pure skip, no pipeline ran — still record skip reason
            db.update_trade_pipeline_columns(
                conn, trade_id,
                final_signal_tier="SKIP",
                skip_reason=s["skip_reason"],
            )
            continue

        stage1 = s.get("stage1", {})
        stage2 = s.get("stage2", {})
        stage4 = s.get("stage4", {})
        metrics = stage2.get("raw_metrics", {})
        thresholds = stage2.get("thresholds", {})

        diagnostics = {
            "hist_range_45d": metrics.get("hist_range_45d"),
            "realized_vol_60d": metrics.get("realized_vol_60d"),
            "sigma_move_val": metrics.get("sigma_move_pct"),
            "actual_price_move": metrics.get("actual_move_pct"),
            "rsi": metrics.get("rsi_14"),
            "iv_percentile": metrics.get("iv_percentile_current"),  # null in v1
            "iv_expansion": None,  # null in v1
            "volume_spike_detected": int(
                bool(metrics.get("volume_spike", {}).get("fired", False))
                if isinstance(metrics.get("volume_spike"), dict) else False
            ),
            "earnings_occurred": 0,  # not detected in v1
            "actual_earnings_move": None,  # null in v1
            "implied_earnings_move": None,  # null in v1
            "legislative_event_occurred": 0,  # Stage 3 may flip this later
            "legislative_event_detail": None,
            "news_catalyst_fired": 0,
            "news_day_move": None,
            "sector_etf_move": metrics.get("sector_etf_move"),
            "sector_vol_60d": metrics.get("sector_vol_60d"),
            "threshold_a1": thresholds.get("a1"),
            "threshold_a2": thresholds.get("a2"),
            "threshold_a3": thresholds.get("a3"),  # null in v1
            "threshold_b1": thresholds.get("b1"),  # null in v1
            "threshold_b3": thresholds.get("b3"),
            "threshold_b4": thresholds.get("b4"),
            "checks_fired": stage2.get("checks_fired", []),
            "verdict": stage2.get("verdict"),
            "outcome_type": "actual",  # this is a real-time score, not retroactive
        }

        try:
            db.upsert_trade_diagnostics(conn, trade_id, diagnostics)
        except Exception as e:
            print(f"  [persist diag error] trade_id={trade_id}: {e}", file=sys.stderr)
            continue

        # Update denormalized columns on the trades row.
        # Pre-Stage-3 tier gets written here; Phase C will overwrite after LLM.
        db.update_trade_pipeline_columns(
            conn, trade_id,
            alignment_multiplier=stage1.get("multiplier"),
            owd_score_a=stage2.get("score_a"),
            owd_score_b=stage2.get("score_b"),
            owd_total=stage2.get("score_total"),
            owd_verdict=stage2.get("verdict"),
            clustering_count=int(stage4.get("cluster_count", 0)),
            cross_party_cluster=int(bool(stage4.get("cross_party"))),
            final_signal_tier=s.get("tier_pre_stage3"),
        )
        n += 1
    return n


# ---------------------------------------------------------------------------
# LLM input selection
# ---------------------------------------------------------------------------

def select_for_llm(scored: List[Dict], max_trades: int = DEFAULT_LLM_TRADE_CAP) -> List[Dict]:
    """
    Pick the top-N trades worth sending to LLM Phase B for Stage 3 catalyst
    research. Selection criteria:

    1. Survives Stage 2 (verdict != closed)
    2. Not a pure skip (must have stage1 + stage2)
    3. Sorted by alignment_multiplier desc, then cluster_count desc
    4. Capped at max_trades

    Trades excluded from LLM still get their Stage 1+2+4 diagnostics
    persisted by persist_diagnostics; they appear in the digest as
    MODERATE/SKIP based on Phase A scoring alone.
    """
    eligible = []
    for s in scored:
        if s.get("skip_reason") and not s.get("stage1"):
            continue
        verdict = s.get("stage2", {}).get("verdict")
        if verdict == "closed":
            continue
        eligible.append(s)

    eligible.sort(
        key=lambda s: (
            -float(s.get("stage1", {}).get("multiplier") or 0),
            -float(s.get("stage4", {}).get("cluster_count") or 0),
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


def render_research_pack(
    scored: List[Dict],
    selected_for_llm: List[Dict],
    lookback_days: int,
    today: str,
) -> str:
    """
    Assemble the markdown research pack the LLM Phase B reads.

    The pack contains:
      - Run summary (counts, tier breakdown, parameter snapshot)
      - "Send to LLM Phase B" section with detailed per-trade context
        for the selected trades (Stage 3 web search budget specified)
      - Pre-Stage-3 tier breakdown table for context
    """
    lines: List[str] = []
    lines.append(f"# Daily Signal — Research Pack ({today})\n")
    lines.append(f"*Lookback window: {lookback_days} days. Total trades scored: {len(scored)}.*\n")

    # ---- Run summary ----
    tier_counts: Dict[str, int] = {"STRONG": 0, "BASE": 0, "MODERATE": 0, "SKIP": 0}
    skip_reasons: Dict[str, int] = {}
    for s in scored:
        tier = s.get("tier_pre_stage3", "SKIP")
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if tier == "SKIP":
            reason = s.get("tier_pre_stage3_reason") or s.get("skip_reason") or "unknown"
            # Bucket reasons
            if "no ticker" in reason:
                key = "no_ticker"
            elif "dependent" in reason:
                key = "dependent_trade"
            elif "opportunity-expired" in reason or "closed" in reason:
                key = "owd_window_closed"
            else:
                key = "other"
            skip_reasons[key] = skip_reasons.get(key, 0) + 1

    lines.append("## Pipeline summary (pre-Stage-3)\n")
    lines.append(f"- **STRONG-eligible:** {tier_counts.get('STRONG', 0)}")
    lines.append(f"- **BASE-eligible:** {tier_counts.get('BASE', 0)}")
    lines.append(f"- **MODERATE-eligible:** {tier_counts.get('MODERATE', 0)}")
    lines.append(f"- **SKIP:** {tier_counts.get('SKIP', 0)}")
    if skip_reasons:
        lines.append("\n**Skip reason histogram:**")
        for k, v in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            lines.append(f"- {k}: {v}")
    lines.append("")

    if not scored:
        lines.append("\n## No trades to score\n")
        lines.append(
            "No trades disclosed in the lookback window. The Daily Signal "
            "agent ran cleanly with nothing to do. This is normal on holidays "
            "or weekends, or if the Data Maintenance agent hasn't pulled new "
            "data recently."
        )
        return "\n".join(lines) + "\n"

    # ---- Trades being sent to LLM ----
    lines.append(f"## Send to LLM (Stage 3 catalyst search): {len(selected_for_llm)} trades\n")
    lines.append("**Web search budget: 5–10 searches per trade, 80 max total.**\n")
    lines.append(
        "For each trade below, search for: (a) upcoming hearings/markups in "
        "the politician's committees; (b) federal contract or budget cycle "
        "events relevant to the ticker (earnings, FDA PDUFA, FERC, contract "
        "awards) within 90 days; (c) any past catalyst since the trade date "
        "that may have already absorbed the edge (for retroactive B2/B3 "
        "scoring).\n"
    )

    for i, s in enumerate(selected_for_llm, 1):
        t = s["_trade"]
        s1 = s.get("stage1", {})
        s2 = s.get("stage2", {})
        s4 = s.get("stage4", {})
        metrics = s2.get("raw_metrics", {})

        lines.append(f"### Trade {i}: {t.get('politician_name')} — {t.get('ticker')} ({t.get('transaction_type')})\n")
        lines.append(f"- **trade_id:** {t.get('id')}")
        lines.append(f"- **trade_date:** {t.get('trade_date')}")
        lines.append(f"- **disclosure_date:** {t.get('disclosure_date')}")
        lines.append(f"- **amount_range:** {t.get('amount_range') or '—'}")
        lines.append(f"- **trader_tag:** {t.get('trader_tag')}")
        lines.append(f"- **sector:** {t.get('sector') or metrics.get('sector') or '—'}")

        lines.append("\n**Stage 1 — Alignment Multiplier**")
        lines.append(f"- multiplier: **{s1.get('multiplier'):.1f}x**" if s1.get('multiplier') is not None else "- multiplier: —")
        lines.append(f"- basis: {s1.get('basis')}")
        lines.append(f"- committee_aligned: {s1.get('committee_aligned')}")
        lines.append(f"- mega_cap_override: {s1.get('mega_cap')}")
        if s1.get("politician_in_db"):
            lines.append(f"- roster_tier: {s1.get('roster_tier') or 'unknown'}")

        lines.append("\n**Stage 2 — Opportunity Window Diagnostic (Lite)**")
        lines.append(f"- score_a: {s2.get('score_a')}, score_b: {s2.get('score_b')}, total: {s2.get('score_total')}")
        lines.append(f"- verdict: **{s2.get('verdict')}**")
        lines.append(f"- checks_fired: {', '.join(s2.get('checks_fired', [])) or 'none'}")
        if metrics.get("entry_price") and metrics.get("current_price"):
            lines.append(
                f"- entry: ${metrics['entry_price']:.2f} → current: ${metrics['current_price']:.2f} "
                f"({_fmt_pct(metrics.get('actual_move_pct'))} over {metrics.get('days_elapsed', 0)} days)"
            )
        if metrics.get("hist_range_45d"):
            lines.append(f"- hist_range_45d: {_fmt_pct(metrics['hist_range_45d'])}, "
                         f"realized_vol_60d: {_fmt_pct(metrics.get('realized_vol_60d'))}")
        if metrics.get("rsi_14"):
            lines.append(f"- RSI(14): {_fmt_num(metrics['rsi_14'], 1)}")
        if metrics.get("sector_etf_move") is not None:
            lines.append(
                f"- sector ETF ({metrics.get('sector_etf')}): "
                f"{_fmt_pct(metrics['sector_etf_move'])} over {metrics.get('sector_days_elapsed', 0)}d"
            )
        if s2.get("skipped_checks"):
            lines.append(f"- *skipped checks (MMD-deferred):* {len(s2['skipped_checks'])}")

        # Earnings calendar (Phase 2.3.5)
        next_earnings = metrics.get("next_earnings_date")
        if next_earnings:
            lines.append(f"\n**Next earnings:** {next_earnings}")
            eps_avg = metrics.get("next_earnings_eps_avg")
            if eps_avg:
                lines.append(f"- analyst EPS estimate: ${eps_avg:.2f} "
                             f"(range ${metrics.get('next_earnings_eps_low', 0):.2f}–"
                             f"${metrics.get('next_earnings_eps_high', 0):.2f})")
            lines.append("- *Stage 3: incorporate this date into your forward "
                         "catalyst search; the options layer below has already "
                         "used it for DTE selection.*")

        lines.append("\n**Stage 4 — Clustering**")
        lines.append(f"- cluster_count: {s4.get('cluster_count')} "
                     f"({s4.get('member_count')} member, {s4.get('spouse_count')} spouse)")
        lines.append(f"- cross_party: {s4.get('cross_party')}")
        lines.append(f"- bumps_tier: {s4.get('bumps_tier')}")
        if s4.get("politicians"):
            other_pols = [p["name"] for p in s4["politicians"]]
            lines.append(f"- other politicians in window: {', '.join(other_pols)}")
        if s4.get("same_sector_count"):
            lines.append(f"- *same-sector buys (broader context):* {s4['same_sector_count']}")

        # ---- Phase 2.3.5: Real options strike (any selected-for-LLM trade) ----
        # Fetch for ALL selected trades, not just STRONG/BASE pre-tier,
        # because Stage 3 can upgrade MODERATE→BASE when it finds a
        # forward catalyst (fix from Phase 2.3.6 post-mortem on the
        # Thomas Kean LIN/AMCR run). The LLM will use the real-chain
        # values if it confirms the upgrade to BASE/STRONG; if it
        # keeps the trade at MODERATE, the block is purely informational.
        pre_tier = s.get("tier_pre_stage3", "MODERATE")
        if pre_tier != "SKIP":
            lines.append("\n**Real-chain options pick (Phase 2.3.5):**")
            try:
                import options_chain
                # Use earnings date as catalyst if we have one and it's
                # within a reasonable forward window. Otherwise let the
                # picker use its default DTE window.
                catalyst_date = next_earnings if next_earnings else None
                has_catalyst = bool(catalyst_date)
                # Pass signal_tier as the pre-tier OR "BASE" if it's
                # MODERATE — so the picker uses BASE's delta target
                # (the likely upgrade tier) instead of MODERATE's "no play".
                chain_tier = pre_tier if pre_tier in ("STRONG", "BASE") else "BASE"
                pick = options_chain.best_strike_for_trade(
                    ticker=t.get("ticker"),
                    signal_tier=chain_tier,
                    has_catalyst=has_catalyst,
                    catalyst_date=catalyst_date,
                    today=today,
                    transaction_type=(t.get("transaction_type") or "buy"),
                )
                if pre_tier == "MODERATE":
                    lines.append(f"- *pre-Stage-3 tier is MODERATE; "
                                 f"chain fetched speculatively in case Stage 3 "
                                 f"upgrades to BASE*")
                if pick.get("mode") == "real":
                    lines.append(f"- structure: **{pick['structure']}** "
                                 f"strike ${pick['strike']}, expiry {pick['expiry']} "
                                 f"({pick['dte']} DTE)")
                    lines.append(f"- delta: {pick['delta']:.2f} "
                                 f"(target {pick['target_delta']:.2f}, "
                                 f"error {pick['delta_error']:.2f})")
                    lines.append(f"- IV: {pick['iv']*100:.1f}% "
                                 f"(computed via inverse BS from lastPrice)")
                    lines.append(f"- gamma: {pick['gamma']:.4f}, "
                                 f"theta: {pick['theta']:+.3f}/day, "
                                 f"vega: {pick['vega']:.3f}")
                    if pick.get("bid", 0) > 0 and pick.get("ask", 0) > 0:
                        lines.append(f"- bid/ask: ${pick['bid']:.2f} / ${pick['ask']:.2f} "
                                     f"(mid ${pick['mid']:.2f})")
                    else:
                        lines.append(f"- last: ${pick.get('lastPrice', 0):.2f} "
                                     f"(off-hours; bid/ask stale)")
                    lines.append(f"- volume: {pick.get('volume', 0)}, "
                                 f"OI: {pick.get('openInterest', 0)}")
                    lines.append(f"- contract: `{pick.get('contractSymbol', '')}`")
                    if pick.get("iv_warning"):
                        lines.append(f"- ⚠️ {pick['iv_warning']}")
                    lines.append(f"- *{pick.get('snapshot_caveat', '')}*")
                else:
                    lines.append(f"- *real-chain unavailable: "
                                 f"{pick.get('fallback_reason', 'unknown')}*")
                    lines.append(f"- conceptual fallback: {pick.get('structure')} "
                                 f"delta {pick.get('delta_target')} DTE {pick.get('dte_window')}")
            except Exception as e:
                lines.append(f"- *options layer error: {e}*")

        lines.append(f"\n**Pre-Stage-3 tier:** `{s.get('tier_pre_stage3')}` "
                     f"({s.get('tier_pre_stage3_reason')})")
        lines.append("")

    # ---- All scored trades (compact table for context) ----
    lines.append("\n## All scored trades (compact view)\n")
    lines.append("| # | Politician | Ticker | Type | Trade Date | Tier | Mult | OWD | Cluster |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for i, s in enumerate(scored, 1):
        t = s["_trade"]
        s1 = s.get("stage1", {})
        s2 = s.get("stage2", {})
        s4 = s.get("stage4", {})
        mult = s1.get("multiplier")
        owd = s2.get("verdict") or "—"
        owd_str = f"{owd}({s2.get('score_total', '?')})"
        cluster = s4.get("cluster_count", 0)
        lines.append(
            f"| {i} | {(t.get('politician_name') or '—')[:25]} "
            f"| {t.get('ticker') or '—'} | {t.get('transaction_type') or '—'} "
            f"| {t.get('trade_date') or '—'} | {s.get('tier_pre_stage3', '?')} "
            f"| {(f'{mult:.1f}x' if mult else '—')} | {owd_str} | {cluster} |"
        )
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Phase C writeback: parse LLM narrative JSON block → persist Stage 3 results
# ---------------------------------------------------------------------------

import re as _re


_JSON_FENCE_RE = _re.compile(r"```json\s*\n(\{.*?\})\s*\n```", _re.DOTALL)


def extract_json_block(narrative_text: str) -> Optional[Dict]:
    """
    Find the final fenced ```json block in the narrative text, parse it,
    and return the dict. Returns None if no block is found or parsing fails.

    The prompt instructs the LLM to emit exactly one fenced JSON block at
    the very end of the narrative. We search for all ```json ... ``` blocks
    and take the LAST one, so any example blocks earlier in the prompt
    or in the LLM's commentary don't confuse us.
    """
    matches = _JSON_FENCE_RE.findall(narrative_text)
    if not matches:
        return None
    for block in reversed(matches):
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and "trades" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def apply_llm_writeback(
    conn,
    narrative_path: str,
    dry_run: bool = False,
) -> Dict:
    """
    Read the LLM narrative from disk, extract the fenced JSON block, and
    persist Stage 3 results + final tier assignments back to the DB.

    For each trade entry in the JSON block:
      1. Look up the trade's Phase A scoring result to get Stage 1, 2, 4
         values (query trades + trade_diagnostics)
      2. Call pipeline.assemble_final_tier() with the LLM's Stage 3
         forward_catalyst result to compute the canonical tier
      3. Update trades.final_signal_tier + forward_catalyst columns
      4. Update trade_diagnostics with the legislative_event_* and
         news_catalyst_* fields if the LLM surfaced a past catalyst
      5. Upsert a row into recommendations with thesis + bear_case +
         the conceptual options structure (if one was suggested)

    Returns a dict summary: {parsed, applied, strong_count, errors}.
    If dry_run is set, the function still parses the JSON and reports
    what it WOULD do but doesn't write to the DB.
    """
    summary: Dict = {
        "parsed": False,
        "applied": 0,
        "strong_count": 0,
        "base_count": 0,
        "moderate_count": 0,
        "skip_count": 0,
        "errors": [],
    }

    try:
        with open(narrative_path) as f:
            narrative = f.read()
    except OSError as e:
        summary["errors"].append(f"read failed: {e}")
        return summary

    parsed = extract_json_block(narrative)
    if not parsed:
        summary["errors"].append("no valid fenced JSON block found in narrative")
        return summary

    summary["parsed"] = True
    trades = parsed.get("trades", [])
    if not isinstance(trades, list):
        summary["errors"].append("JSON block 'trades' field is not a list")
        return summary

    now = datetime.utcnow().isoformat()

    for entry in trades:
        if not isinstance(entry, dict):
            continue
        trade_id = entry.get("trade_id")
        if trade_id is None:
            summary["errors"].append(f"entry missing trade_id: {entry}")
            continue

        # Look up the Phase A scoring output stored on the trades row
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT id, alignment_multiplier, owd_score_a, owd_score_b,
                          owd_total, owd_verdict, clustering_count, cross_party_cluster,
                          final_signal_tier as phase_a_tier, transaction_type,
                          politician_name, ticker, trade_date, trader_tag, sector
                   FROM trades WHERE id = ?""",
                (trade_id,),
            )
            row = cur.fetchone()
            if not row:
                summary["errors"].append(f"trade_id {trade_id} not found in DB")
                continue
            trade_row = dict(row)
        except Exception as e:
            summary["errors"].append(f"trade_id {trade_id} lookup failed: {e}")
            continue

        forward_catalyst = entry.get("forward_catalyst")
        forward_catalyst_date = entry.get("forward_catalyst_date")
        past_catalyst = entry.get("past_catalyst_evidence")
        thesis = entry.get("thesis") or ""
        bear_case = entry.get("bear_case") or ""
        conceptual = entry.get("conceptual_play") or {}
        llm_suggested_tier = entry.get("suggested_tier")

        # Reconstruct stage dicts for assemble_final_tier
        stage1 = {
            "multiplier": float(trade_row.get("alignment_multiplier") or 0.3),
            "basis": "phase-a",
        }
        stage2 = {
            "verdict": trade_row.get("owd_verdict") or "open",
            "score_total": int(trade_row.get("owd_total") or 0),
        }
        stage4 = {
            "cluster_count": float(trade_row.get("clustering_count") or 0),
            "cross_party": bool(trade_row.get("cross_party_cluster")),
            "bumps_tier": (
                float(trade_row.get("clustering_count") or 0) >= 3.0
                or bool(trade_row.get("cross_party_cluster"))
            ),
        }
        stage3_catalyst = None
        if forward_catalyst:
            stage3_catalyst = {
                "forward_catalyst": forward_catalyst,
                "catalyst_date": forward_catalyst_date,
            }

        tier_result = pipeline.assemble_final_tier(
            stage1, stage2, stage3_catalyst, stage4
        )
        final_tier = tier_result["tier"]

        # If the LLM surfaced a past catalyst that retroactively closes the
        # window, honor the LLM's "SKIP" suggestion
        if llm_suggested_tier == "SKIP" and past_catalyst:
            final_tier = "SKIP"

        if not dry_run:
            try:
                # Update trades row with Stage 3 data + final tier
                db.update_trade_pipeline_columns(
                    conn, trade_id,
                    forward_catalyst=forward_catalyst,
                    final_signal_tier=final_tier,
                )

                # If past catalyst evidence surfaced, update trade_diagnostics
                if past_catalyst:
                    cur.execute(
                        """UPDATE trade_diagnostics SET
                             legislative_event_occurred = 1,
                             legislative_event_detail = ?
                           WHERE trade_id = ?""",
                        (past_catalyst, trade_id),
                    )

                # Insert into recommendations for actionable tiers
                if final_tier in ("STRONG", "BASE"):
                    rec = {
                        "signal_tier": final_tier,
                        "option_type": "conceptual",
                        "strike": None,
                        "expiry": conceptual.get("target_expiry_iso"),
                        "dte": None,
                        "delta": None,
                        "thesis": thesis,
                        "bear_case": bear_case,
                        "snapshot_caveat": (
                            f"Conceptual only (no MMD). Phase 2.3.5 will add "
                            f"real Greeks. As of {now}."
                        ),
                    }
                    # Add DTE if we can parse it from the min_dte field
                    if conceptual.get("min_dte") and conceptual.get("max_dte"):
                        rec["dte"] = (conceptual["min_dte"] + conceptual["max_dte"]) // 2
                    db.upsert_recommendation(conn, trade_id, rec)

                conn.commit()
            except Exception as e:
                summary["errors"].append(f"trade_id {trade_id} persist failed: {e}")
                continue

        # Tally
        summary["applied"] += 1
        if final_tier == "STRONG":
            summary["strong_count"] += 1
        elif final_tier == "BASE":
            summary["base_count"] += 1
        elif final_tier == "MODERATE":
            summary["moderate_count"] += 1
        else:
            summary["skip_count"] += 1

    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Phase A driver for Daily Signal agent")
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help=f"Trailing window in days (default {DEFAULT_LOOKBACK_DAYS})")
    ap.add_argument("--limit-trades", type=int, default=DEFAULT_LLM_TRADE_CAP,
                    help=f"Max trades to send to LLM Phase B (default {DEFAULT_LLM_TRADE_CAP})")
    ap.add_argument("--out", type=str, default=None,
                    help="Output path for the research pack markdown")
    ap.add_argument("--dry-run", action="store_true",
                    help="Skip DB writes")
    ap.add_argument("--no-persist", action="store_true",
                    help="Skip DB persistence even outside dry-run (alias)")
    ap.add_argument("--writeback", type=str, default=None, metavar="NARRATIVE_PATH",
                    help="Post-LLM Phase C: parse the narrative at this path and "
                         "persist Stage 3 results + recommendations to the DB. "
                         "When this flag is set, all other Phase A arguments are "
                         "ignored.")
    args = ap.parse_args()

    # ---- Phase C (writeback) mode: parse LLM narrative and persist ----
    if args.writeback:
        conn = db.connect()
        print(f"[daily-signal] writeback mode — reading {args.writeback}",
              file=sys.stderr)
        summary = apply_llm_writeback(conn, args.writeback, dry_run=args.dry_run)
        print(f"[daily-signal] writeback: parsed={summary['parsed']}, "
              f"applied={summary['applied']}, "
              f"STRONG={summary['strong_count']}, "
              f"BASE={summary['base_count']}, "
              f"MODERATE={summary['moderate_count']}, "
              f"SKIP={summary['skip_count']}",
              file=sys.stderr)
        if summary["errors"]:
            print(f"[daily-signal] writeback errors: {len(summary['errors'])}",
                  file=sys.stderr)
            for err in summary["errors"][:10]:
                print(f"  - {err}", file=sys.stderr)
        conn.close()
        return 0 if summary["parsed"] or summary["applied"] >= 0 else 1

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    print(f"[daily-signal] start — {timestamp}", file=sys.stderr)

    conn = db.connect()

    # ---- Fetch overnight trades ----
    trades = fetch_overnight(conn, lookback_days=args.lookback)
    print(f"[daily-signal] fetched {len(trades)} overnight trades "
          f"(lookback={args.lookback}d)", file=sys.stderr)

    if not trades:
        # Still produce a research pack so the runner has something
        pack = render_research_pack([], [], args.lookback, today)
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(pack)
            print(f"[daily-signal] wrote research pack: {out_path} (empty run)", file=sys.stderr)
        else:
            sys.stdout.write(pack)
        backtest.save_price_cache()
        return 0

    # ---- Score Stages 1+2+4 ----
    print(f"[daily-signal] scoring {len(trades)} trades through Stages 1, 2, 4...",
          file=sys.stderr)
    scored = score_all(conn, trades)

    # ---- Persist diagnostics ----
    persist = not (args.dry_run or args.no_persist)
    if persist:
        n_persisted = persist_diagnostics(conn, scored, dry_run=False)
        print(f"[daily-signal] persisted {n_persisted} diagnostics rows", file=sys.stderr)
    else:
        print("[daily-signal] (dry-run / --no-persist — no DB writes)", file=sys.stderr)

    # ---- Select for LLM ----
    selected = select_for_llm(scored, max_trades=args.limit_trades)
    print(f"[daily-signal] selected {len(selected)} trades for LLM Phase B", file=sys.stderr)

    # ---- Render research pack ----
    pack = render_research_pack(scored, selected, args.lookback, today)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(pack)
        print(f"[daily-signal] wrote research pack: {out_path} ({len(pack):,} bytes)",
              file=sys.stderr)
    else:
        sys.stdout.write(pack)

    backtest.save_price_cache()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
