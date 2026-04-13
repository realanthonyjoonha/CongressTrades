#!/usr/bin/env python3
"""
feedback_loop.py — Phase 3 parameter optimization engine.

Implements the weekly feedback loop from specs/04-feedback.md:
  1. Backfill outcomes: compute 30/60/90-day excess return for scored trades
  2. False negative rate: % of skipped/downgraded trades that would have won
  3. False positive rate: % of recommended trades that lost money
  4. Per-check correlation: how well each OWD check predicts losses
  5. Sensitivity analysis: FN/FP rates at k_XX ± 10% and ± 20%
  6. Propose adjustments with confidence qualifiers

The output is a "Parameter Health" markdown section for the Weekly Deep
Research narrative, plus structured data for the parameter_changelog.
No parameter changes are auto-applied — the user approves or rejects.

Requires real production data in trade_diagnostics. Returns
"insufficient data (N=0)" gracefully if the table is empty. The system
learns from its own results once the Daily Signal agent has been running
for ≥2 weeks (to accumulate enough 30-day outcomes).

CLI:
    python3 scripts/feedback_loop.py                  # full analysis
    python3 scripts/feedback_loop.py --backfill-only  # just update outcomes
    python3 scripts/feedback_loop.py --dry-run        # no DB writes
    python3 scripts/feedback_loop.py --out report.md  # write markdown
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import backtest  # noqa: E402

# Minimum samples required for meaningful statistics
MIN_SAMPLES_ANALYSIS = 10
MIN_SAMPLES_PER_CHECK = 5

# Outcome horizon (days)
OUTCOME_HORIZONS = [30, 60, 90]
PRIMARY_HORIZON = 60  # the one used for FN/FP rates


# ---------------------------------------------------------------------------
# Step 1: Backfill outcomes
# ---------------------------------------------------------------------------

def backfill_outcomes(
    conn,
    max_rows: int = 1000,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    For each row in trade_diagnostics that has no outcome_pnl_60d,
    compute the 30/60/90-day excess return from the trade date via
    yfinance (backtest.compute_excess_return). Updates in place.

    Returns {backfilled, skipped, errors}.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT td.id, td.trade_id, t.ticker, t.trade_date, t.transaction_type
        FROM trade_diagnostics td
        JOIN trades t ON t.id = td.trade_id
        WHERE td.outcome_pnl_60d IS NULL
        LIMIT ?
        """,
        (max_rows,),
    )
    rows = cur.fetchall()

    stats = {"backfilled": 0, "skipped": 0, "errors": 0}
    for row in rows:
        diag_id = row["id"]
        ticker = row["ticker"]
        trade_date = row["trade_date"]
        txn_type = (row["transaction_type"] or "buy").lower()

        try:
            pnl = {}
            for days in OUTCOME_HORIZONS:
                excess = backtest.compute_excess_return(
                    ticker, trade_date, days=days
                )
                if excess is not None:
                    # For sell trades, the "profit" is the inverse
                    if txn_type == "sell":
                        excess = -excess
                pnl[f"outcome_pnl_{days}d"] = excess

            if pnl.get("outcome_pnl_60d") is None:
                stats["skipped"] += 1
                continue

            if not dry_run:
                cur.execute(
                    """
                    UPDATE trade_diagnostics SET
                        outcome_pnl_30d = ?,
                        outcome_pnl_60d = ?,
                        outcome_pnl_90d = ?,
                        outcome_type = 'actual'
                    WHERE id = ?
                    """,
                    (
                        pnl.get("outcome_pnl_30d"),
                        pnl.get("outcome_pnl_60d"),
                        pnl.get("outcome_pnl_90d"),
                        diag_id,
                    ),
                )
            stats["backfilled"] += 1
        except Exception as e:
            print(f"  [feedback] backfill error diag_id={diag_id}: {e}",
                  file=sys.stderr)
            stats["errors"] += 1

    if not dry_run:
        conn.commit()
    return stats


# ---------------------------------------------------------------------------
# Step 2: False negative rate
# ---------------------------------------------------------------------------

def compute_fn_rate(
    conn,
    horizon_days: int = PRIMARY_HORIZON,
) -> Dict[str, Any]:
    """
    Among trades that were scored "window-closed" (verdict='closed')
    or skipped, what % would have been profitable at the given horizon?

    A high FN rate means the pipeline is too aggressive at filtering —
    it's throwing away winners.

    Returns {fn_rate, n_filtered, n_profitable, profitable_avg_excess,
    confidence, detail}.
    """
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT td.*, t.ticker, t.politician_name, t.trade_date, t.final_signal_tier
        FROM trade_diagnostics td
        JOIN trades t ON t.id = td.trade_id
        WHERE td.verdict = 'closed'
          AND td.outcome_pnl_{horizon_days}d IS NOT NULL
        """,
    )
    rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return {"fn_rate": None, "n_filtered": 0, "confidence": "none"}

    n_profitable = sum(
        1 for r in rows
        if (r.get(f"outcome_pnl_{horizon_days}d") or 0) > 0
    )
    profitable_excesses = [
        r[f"outcome_pnl_{horizon_days}d"]
        for r in rows
        if (r.get(f"outcome_pnl_{horizon_days}d") or 0) > 0
    ]

    fn_rate = n_profitable / len(rows) if rows else 0.0
    avg_excess = (
        statistics.mean(profitable_excesses) if profitable_excesses else 0.0
    )
    confidence = _confidence_label(len(rows))

    return {
        "fn_rate": fn_rate,
        "n_filtered": len(rows),
        "n_profitable": n_profitable,
        "profitable_avg_excess": avg_excess,
        "confidence": confidence,
        "horizon_days": horizon_days,
    }


# ---------------------------------------------------------------------------
# Step 3: False positive rate
# ---------------------------------------------------------------------------

def compute_fp_rate(
    conn,
    horizon_days: int = PRIMARY_HORIZON,
) -> Dict[str, Any]:
    """
    Among trades that passed the pipeline (verdict='open' or 'narrowing')
    and were recommended (final_signal_tier in STRONG/BASE), what %
    lost money at the given horizon?

    A high FP rate means the pipeline is too permissive — it's
    recommending losers.

    Returns {fp_rate, n_recommended, n_lost_money, loss_avg_excess,
    confidence, detail}.
    """
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT td.*, t.ticker, t.politician_name, t.trade_date, t.final_signal_tier
        FROM trade_diagnostics td
        JOIN trades t ON t.id = td.trade_id
        WHERE td.verdict IN ('open', 'narrowing')
          AND t.final_signal_tier IN ('STRONG', 'BASE')
          AND td.outcome_pnl_{horizon_days}d IS NOT NULL
        """,
    )
    rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return {"fp_rate": None, "n_recommended": 0, "confidence": "none"}

    n_lost = sum(
        1 for r in rows
        if (r.get(f"outcome_pnl_{horizon_days}d") or 0) < 0
    )
    loss_excesses = [
        r[f"outcome_pnl_{horizon_days}d"]
        for r in rows
        if (r.get(f"outcome_pnl_{horizon_days}d") or 0) < 0
    ]

    fp_rate = n_lost / len(rows) if rows else 0.0
    avg_loss = statistics.mean(loss_excesses) if loss_excesses else 0.0
    confidence = _confidence_label(len(rows))

    return {
        "fp_rate": fp_rate,
        "n_recommended": len(rows),
        "n_lost_money": n_lost,
        "loss_avg_excess": avg_loss,
        "confidence": confidence,
        "horizon_days": horizon_days,
    }


# ---------------------------------------------------------------------------
# Step 4: Per-check correlation
# ---------------------------------------------------------------------------

def per_check_correlation(
    conn,
    horizon_days: int = PRIMARY_HORIZON,
) -> List[Dict[str, Any]]:
    """
    For each OWD check (A1-A4, B1-B4), compute the correlation between
    that check firing and the trade being unprofitable.

    Positive correlation = check firing correctly predicts losses.
    Negative correlation = check fires on winners (bad signal).
    Near-zero = check is uninformative.

    Returns list of {check, fired_count, not_fired_count,
    fired_loss_rate, not_fired_loss_rate, correlation_direction,
    confidence}.
    """
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT td.checks_fired, td.outcome_pnl_{horizon_days}d as pnl
        FROM trade_diagnostics td
        WHERE td.outcome_pnl_{horizon_days}d IS NOT NULL
          AND td.checks_fired IS NOT NULL
        """,
    )
    rows = cur.fetchall()
    if not rows:
        return []

    all_checks = ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4"]
    results: List[Dict] = []

    for check in all_checks:
        fired_losses = 0
        fired_total = 0
        not_fired_losses = 0
        not_fired_total = 0

        for row in rows:
            try:
                fired_list = json.loads(row["checks_fired"]) if isinstance(row["checks_fired"], str) else (row["checks_fired"] or [])
            except (json.JSONDecodeError, TypeError):
                fired_list = []

            pnl = row["pnl"] or 0
            if check in fired_list:
                fired_total += 1
                if pnl < 0:
                    fired_losses += 1
            else:
                not_fired_total += 1
                if pnl < 0:
                    not_fired_losses += 1

        fired_loss_rate = (fired_losses / fired_total) if fired_total else None
        not_fired_loss_rate = (not_fired_losses / not_fired_total) if not_fired_total else None

        # Direction: positive correlation means the check correctly predicts losses
        if fired_loss_rate is not None and not_fired_loss_rate is not None:
            diff = fired_loss_rate - not_fired_loss_rate
            if diff > 0.05:
                direction = "positive (check predicts losses — good signal)"
            elif diff < -0.05:
                direction = "NEGATIVE (check fires on winners — bad signal)"
            else:
                direction = "neutral (uninformative)"
        else:
            direction = "insufficient data"

        results.append({
            "check": check,
            "fired_count": fired_total,
            "not_fired_count": not_fired_total,
            "fired_loss_rate": fired_loss_rate,
            "not_fired_loss_rate": not_fired_loss_rate,
            "correlation_direction": direction,
            "confidence": _confidence_label(fired_total),
        })

    return results


# ---------------------------------------------------------------------------
# Step 5: Sensitivity analysis
# ---------------------------------------------------------------------------

def sensitivity_analysis(
    conn,
    horizon_days: int = PRIMARY_HORIZON,
) -> List[Dict[str, Any]]:
    """
    For each tunable k_XX parameter, estimate FN/FP rates at
    current value, ±10%, and ±20%.

    This is a simplified version: instead of re-running the full
    pipeline at each parameter variant (expensive), we use the
    raw diagnostic values already stored to estimate what WOULD
    have happened. For A1 (range consumption), we check how many
    trades' actual_price_move / hist_range_45d crosses the threshold
    at the variant parameter value.

    Returns list of {param, current_value, variants: [{delta_pct,
    variant_value, estimated_fn_rate, estimated_fp_rate}]}.
    """
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT td.*, t.final_signal_tier, t.ticker
        FROM trade_diagnostics td
        JOIN trades t ON t.id = td.trade_id
        WHERE td.outcome_pnl_{horizon_days}d IS NOT NULL
        """,
    )
    rows = [dict(r) for r in cur.fetchall()]

    if len(rows) < MIN_SAMPLES_ANALYSIS:
        return []

    params = db.all_params(conn)
    if not params:
        return []

    results: List[Dict] = []

    # For each k_XX, estimate the impact of changing it
    for param_name, current_value in params.items():
        if not param_name.startswith("k_") or current_value is None:
            continue

        variants = []
        for delta_pct in [-20, -10, 0, 10, 20]:
            variant_value = current_value * (1 + delta_pct / 100.0)
            # Count how many trades' check outcomes would change
            # This is parameter-specific logic
            # For simplicity in v1: report the parameter name and
            # variant values for the LLM's narrative — actual re-scoring
            # at variant values is Phase 3.5 work
            variants.append({
                "delta_pct": delta_pct,
                "variant_value": round(variant_value, 4),
            })

        results.append({
            "param": param_name,
            "current_value": current_value,
            "variants": variants,
            "n_samples": len(rows),
            "note": (
                "Full sensitivity requires re-scoring at each variant — "
                "deferred to Phase 3.5. These are the variant values only."
            ),
        })

    return results


# ---------------------------------------------------------------------------
# Step 6: Propose adjustments
# ---------------------------------------------------------------------------

def propose_adjustments(
    fn_analysis: Dict,
    fp_analysis: Dict,
    check_correlations: List[Dict],
    params: Dict[str, float],
) -> List[Dict[str, Any]]:
    """
    Combine all analysis into concrete parameter adjustment proposals.

    Rules:
    - If FN rate > k_fn_threshold (default 0.30): thresholds are too
      tight → propose loosening high-correlation checks by 10%
    - If FP rate > k_fp_threshold (default 0.40): thresholds are too
      loose → propose tightening by 10%
    - If a check has negative correlation (fires on winners): flag for
      review, propose weight reduction or removal

    Returns list of proposals: [{param, current, proposed, rationale,
    confidence, fn_impact, fp_impact}].
    """
    proposals: List[Dict] = []

    fn_rate = fn_analysis.get("fn_rate")
    fp_rate = fp_analysis.get("fp_rate")
    fn_threshold = params.get("k_fn_threshold", 0.30)
    fp_threshold = params.get("k_fp_threshold", 0.40)

    if fn_rate is None or fp_rate is None:
        return [{
            "type": "info",
            "message": (
                "Insufficient data for parameter proposals. Need "
                f"at least {MIN_SAMPLES_ANALYSIS} diagnosed trades "
                "with outcomes. Run the daily pipeline for 2+ weeks "
                "to accumulate data."
            ),
        }]

    # Check if FN or FP rates breach thresholds
    if fn_rate > fn_threshold:
        # Too many false negatives — pipeline filters too aggressively
        # Look for high-correlation checks to loosen
        for cc in check_correlations:
            if (
                cc.get("correlation_direction", "").startswith("positive")
                and cc.get("fired_count", 0) >= MIN_SAMPLES_PER_CHECK
            ):
                check = cc["check"]
                # Map check to parameter
                param_map = {
                    "A1": "k_A1", "A2": "k_A2", "A4": "k_A4_mult",
                    "B3": "k_B3", "B4": "k_B4",
                }
                param = param_map.get(check)
                if param and param in params:
                    current = params[param]
                    # Loosen by 10% (for k_XX: higher threshold = harder to fire)
                    proposed = round(current * 1.10, 4)
                    proposals.append({
                        "type": "loosen",
                        "param": param,
                        "current": current,
                        "proposed": proposed,
                        "rationale": (
                            f"FN rate {fn_rate*100:.1f}% exceeds threshold "
                            f"{fn_threshold*100:.1f}%. Check {check} has "
                            f"positive loss correlation with {cc['fired_count']} "
                            f"samples. Propose loosening {param} by 10% to "
                            f"reduce filtering."
                        ),
                        "confidence": cc["confidence"],
                    })

    if fp_rate > fp_threshold:
        # Too many false positives — pipeline is too permissive
        for cc in check_correlations:
            if (
                cc.get("correlation_direction", "").startswith("NEGATIVE")
                and cc.get("fired_count", 0) >= MIN_SAMPLES_PER_CHECK
            ):
                check = cc["check"]
                proposals.append({
                    "type": "review",
                    "param": None,
                    "rationale": (
                        f"FP rate {fp_rate*100:.1f}% exceeds threshold "
                        f"{fp_threshold*100:.1f}%. Check {check} has NEGATIVE "
                        f"loss correlation (fires on winners) with "
                        f"{cc['fired_count']} samples. Consider reducing its "
                        f"weight or raising the threshold."
                    ),
                    "confidence": cc["confidence"],
                })

    if not proposals:
        proposals.append({
            "type": "stable",
            "message": (
                f"All parameters in stable zone. "
                f"FN rate: {fn_rate*100:.1f}% "
                f"(threshold {fn_threshold*100:.0f}%), "
                f"FP rate: {fp_rate*100:.1f}% "
                f"(threshold {fp_threshold*100:.0f}%). "
                f"No adjustments proposed."
            ),
        })

    return proposals


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def _confidence_label(n: int) -> str:
    if n >= 50:
        return "high"
    if n >= 20:
        return "moderate"
    if n >= MIN_SAMPLES_PER_CHECK:
        return "low"
    return "insufficient"


def render_parameter_health(
    fn_analysis: Dict,
    fp_analysis: Dict,
    check_correlations: List[Dict],
    sensitivity: List[Dict],
    proposals: List[Dict],
    backfill_stats: Dict,
) -> str:
    """
    Render the "Parameter Health" markdown section for the Weekly Deep
    Research narrative. This is inserted by weekly_deep.py or by the
    scheduled task prompt.
    """
    lines: List[str] = []
    lines.append("## Parameter Health (Phase 3 Feedback Loop)\n")

    # Backfill stats
    lines.append("### Outcome backfill\n")
    lines.append(
        f"- Backfilled: {backfill_stats.get('backfilled', 0)} trades\n"
        f"- Skipped (no price data): {backfill_stats.get('skipped', 0)}\n"
        f"- Errors: {backfill_stats.get('errors', 0)}\n"
    )

    # FN rate
    lines.append("### False negative rate (are we filtering out winners?)\n")
    fn_rate = fn_analysis.get("fn_rate")
    if fn_rate is not None:
        lines.append(
            f"- **FN rate: {fn_rate*100:.1f}%** "
            f"({fn_analysis['n_profitable']}/{fn_analysis['n_filtered']} "
            f"skipped trades were profitable at "
            f"{fn_analysis['horizon_days']}d)\n"
        )
        if fn_analysis["n_profitable"] > 0:
            lines.append(
                f"- Average excess of profitable filtered trades: "
                f"+{fn_analysis['profitable_avg_excess']:.1f}%\n"
            )
        lines.append(f"- Confidence: {fn_analysis['confidence']}\n")
    else:
        lines.append("- *Insufficient data. Need more diagnosed trades with outcomes.*\n")

    # FP rate
    lines.append("### False positive rate (are we recommending losers?)\n")
    fp_rate = fp_analysis.get("fp_rate")
    if fp_rate is not None:
        lines.append(
            f"- **FP rate: {fp_rate*100:.1f}%** "
            f"({fp_analysis['n_lost_money']}/{fp_analysis['n_recommended']} "
            f"recommended trades lost money at "
            f"{fp_analysis['horizon_days']}d)\n"
        )
        if fp_analysis["n_lost_money"] > 0:
            lines.append(
                f"- Average excess of losing recommended trades: "
                f"{fp_analysis['loss_avg_excess']:.1f}%\n"
            )
        lines.append(f"- Confidence: {fp_analysis['confidence']}\n")
    else:
        lines.append("- *Insufficient data. Need more recommended trades with outcomes.*\n")

    # Per-check correlation
    if check_correlations:
        active_checks = [c for c in check_correlations if c["fired_count"] > 0]
        if active_checks:
            lines.append("### Per-check loss correlation\n")
            lines.append("| Check | Fired | Loss rate (fired) | Loss rate (not fired) | Direction |")
            lines.append("|---|---|---|---|---|")
            for cc in active_checks:
                flr = f"{cc['fired_loss_rate']*100:.1f}%" if cc["fired_loss_rate"] is not None else "—"
                nflr = f"{cc['not_fired_loss_rate']*100:.1f}%" if cc["not_fired_loss_rate"] is not None else "—"
                lines.append(
                    f"| {cc['check']} | {cc['fired_count']} | {flr} | {nflr} "
                    f"| {cc['correlation_direction'][:30]}... |"
                )
            lines.append("")

    # Proposals
    lines.append("### Proposed adjustments\n")
    for p in proposals:
        if p.get("type") == "info" or p.get("type") == "stable":
            lines.append(f"- {p.get('message')}\n")
        elif p.get("type") == "loosen":
            lines.append(
                f"- **{p['param']}**: {p['current']} → {p['proposed']} "
                f"(loosen by 10%). Confidence: {p.get('confidence', '?')}.\n"
                f"  Rationale: {p['rationale']}\n"
            )
        elif p.get("type") == "review":
            lines.append(
                f"- **Review needed**: {p['rationale']}. "
                f"Confidence: {p.get('confidence', '?')}.\n"
            )

    lines.append(
        "\n*Parameter changes are never auto-applied. Approve or reject "
        "via `db.set_param()` which logs to `parameter_changelog` with "
        "audit trail.*\n"
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_full_analysis(
    conn,
    dry_run: bool = False,
) -> Tuple[str, Dict]:
    """
    Run the complete feedback loop:
      1. Backfill outcomes
      2. Compute FN/FP rates
      3. Per-check correlation
      4. Sensitivity analysis
      5. Propose adjustments
      6. Render Parameter Health markdown

    Returns (markdown_section, structured_analysis_dict).
    """
    print("[feedback] starting full analysis...", file=sys.stderr)

    # Step 1
    backfill = backfill_outcomes(conn, dry_run=dry_run)
    print(f"[feedback] backfill: {backfill}", file=sys.stderr)

    # Step 2
    fn = compute_fn_rate(conn)
    print(f"[feedback] FN rate: {fn.get('fn_rate')}", file=sys.stderr)

    # Step 3
    fp = compute_fp_rate(conn)
    print(f"[feedback] FP rate: {fp.get('fp_rate')}", file=sys.stderr)

    # Step 4
    correlations = per_check_correlation(conn)

    # Step 5
    sensitivity = sensitivity_analysis(conn)

    # Step 6
    params = db.all_params(conn)
    proposals = propose_adjustments(fn, fp, correlations, params)

    # Render
    md = render_parameter_health(fn, fp, correlations, sensitivity, proposals, backfill)

    analysis = {
        "backfill": backfill,
        "fn_rate": fn,
        "fp_rate": fp,
        "check_correlations": correlations,
        "sensitivity": sensitivity,
        "proposals": proposals,
    }

    return md, analysis


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 3 feedback loop")
    ap.add_argument("--backfill-only", action="store_true",
                    help="Only backfill outcomes, skip analysis")
    ap.add_argument("--dry-run", action="store_true",
                    help="No DB writes")
    ap.add_argument("--out", type=str, default=None,
                    help="Write Parameter Health markdown to file")
    args = ap.parse_args()

    conn = db.connect()

    if args.backfill_only:
        stats = backfill_outcomes(conn, dry_run=args.dry_run)
        print(f"[feedback] backfill: {stats}")
        backtest.save_price_cache()
        return 0

    md, analysis = run_full_analysis(conn, dry_run=args.dry_run)

    if args.out:
        from pathlib import Path
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md)
        print(f"[feedback] wrote: {out_path} ({len(md):,} bytes)")
    else:
        sys.stdout.write(md)

    # Also print structured summary
    print(f"\n[feedback] Proposals: {len(analysis['proposals'])}",
          file=sys.stderr)
    for p in analysis["proposals"]:
        ptype = p.get("type", "?")
        if ptype in ("info", "stable"):
            print(f"  [{ptype}] {p.get('message', '')[:80]}", file=sys.stderr)
        elif ptype == "loosen":
            print(f"  [{ptype}] {p['param']}: {p['current']} → {p['proposed']}",
                  file=sys.stderr)
        elif ptype == "review":
            print(f"  [{ptype}] {p.get('rationale', '')[:80]}", file=sys.stderr)

    backtest.save_price_cache()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
