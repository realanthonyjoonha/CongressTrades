#!/usr/bin/env python3
"""
pipeline.py — 4-stage signal scoring engine for the Daily Signal agent.

Implements Stages 1, 2 (Lite), and 4 from specs/02-pipeline.md and
specs/03-diagnostics.md. Stage 3 (forward catalyst search) is LLM-driven
and lives in prompts/daily_signal.md; this module exposes a final-tier
assembly function that takes the LLM's Stage 3 results and produces the
canonical {STRONG, BASE, MODERATE, SKIP} verdict.

Stage 1 — Alignment Multiplier      → compute_alignment_multiplier()
Stage 2 — Opportunity Window (Lite) → compute_owd_score()
Stage 3 — Forward Catalyst          → assemble_final_tier() consumes LLM output
Stage 4 — Clustering Check          → find_clustering()

Top-level orchestrator: score_trade(conn, trade) runs Stages 1, 2, 4
and returns a merged dict ready for the trade_diagnostics table.
assemble_final_tier() is called separately after Stage 3 results are in.

Phase 2.3 ships "Stage 2 Lite" — checks A1, A2, A4, B3, B4 (5 of 8).
A3 (IV expansion) and B1 (earnings pass-through) require MMD options
data and will be added in Phase 2.3.5. The same thresholds apply;
v1 is more conservative (under-flag rather than over-flag).
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# Local imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402
import backtest  # noqa: E402
import sectors  # noqa: E402
import stock_metrics  # noqa: E402

# ---------------------------------------------------------------------------
# Constants (defaults — overridden by db.tunable_parameters at runtime)
# ---------------------------------------------------------------------------

DEFAULT_PARAMS = {
    "k_A1": 0.60,
    "k_A2": 1.50,
    "k_A3": 0.40,           # MMD-deferred in v1
    "k_A4_window": 5,
    "k_A4_mult": 2.0,
    "k_B1": 1.0,            # MMD-deferred in v1
    "k_B3": 1.0,
    "k_B4": 1.5,
    "k_cluster_window": 30,
    "threshold_open": 1,
    "threshold_narrowing": 3,
}

# Tier constants
TIER_STRONG = "STRONG"
TIER_BASE = "BASE"
TIER_MODERATE = "MODERATE"
TIER_SKIP = "SKIP"

# OWD verdicts
OWD_OPEN = "open"
OWD_NARROWING = "narrowing"
OWD_CLOSED = "closed"


def load_params(conn) -> Dict[str, float]:
    """Pull all tunable parameters from DB; fall back to DEFAULT_PARAMS."""
    out = dict(DEFAULT_PARAMS)
    for name in DEFAULT_PARAMS.keys():
        try:
            v = db.get_param(conn, name, default=DEFAULT_PARAMS[name])
            if v is not None:
                out[name] = v
        except Exception:
            pass  # use default
    return out


# ---------------------------------------------------------------------------
# Stage 1 — Committee Alignment Multiplier
# ---------------------------------------------------------------------------

def compute_alignment_multiplier(conn, trade: Dict) -> Dict:
    """
    Stage 1 from specs/02-pipeline.md.

    Returns dict with:
      - multiplier: float in {1.0, 0.7, 0.5, 0.3}
      - basis: human-readable string
      - committee_aligned: bool
      - mega_cap: bool
      - leadership_floor_applied: bool

    Multiplier table (per spec):
      1.0x — ticker sector maps to trader's committee
      1.0x — mega-cap override + trader on a mapped committee
      0.5x — mega-cap override + trader NOT on a mapped committee ("tangential")
      0.7x — chamber-wide leadership floor (Speaker, whip, etc) on all trades
      0.7x — committee-specific leadership (chair, ranking) within own committee
      0.3x — off-committee, non-leadership, non-override

    LIMITATION (v1): The politicians.committee_history and leadership_history
    columns are not yet populated by Phase 0 backtest. Until they are, we
    treat alignment as "is the ticker sector mapped to ANY committee in the
    static config/committees.json?" — a coarse approximation that
    over-includes politicians whose actual committees we don't know. The
    over-inclusion is the conservative direction (gives more politicians a
    1.0x rather than 0.3x); the feedback loop in Phase 3 will tighten this.
    """
    ticker = trade.get("ticker") or ""
    sector = trade.get("sector")
    politician = trade.get("politician_name") or ""

    # Lazy sector classification if missing
    if not sector:
        sector = sectors.classify_ticker(ticker)

    # Check 1: is the ticker on the mega-cap override list?
    mega_cap = db.is_mega_cap_override(conn, ticker)

    # Check 2: is the trade committee-aligned via sector mapping?
    # We reuse backtest.is_aligned which handles all the rules.
    aligned = backtest.is_aligned(conn, ticker, sector)

    # Check 3: politician record + roster tier (currently empty in v1)
    pol_row = None
    pol_committees: List[str] = []
    pol_leadership: List[Dict] = []
    pol_roster_tier: Optional[str] = None
    try:
        row = db.get_politician(conn, politician)
        if row:
            pol_row = dict(row)
            pol_roster_tier = pol_row.get("roster_tier")
            ch = pol_row.get("committee_history")
            if ch:
                try:
                    pol_committees = json.loads(ch)
                except (json.JSONDecodeError, TypeError):
                    pol_committees = []
            lh = pol_row.get("leadership_history")
            if lh:
                try:
                    pol_leadership = json.loads(lh)
                except (json.JSONDecodeError, TypeError):
                    pol_leadership = []
    except Exception:
        pass

    # ---- Apply multiplier rules ----
    multiplier = 0.3  # default off-committee
    basis = "off-committee"
    leadership_floor = False

    if aligned and not mega_cap:
        multiplier = 1.0
        basis = "sector-aligned"
    elif aligned and mega_cap:
        multiplier = 1.0
        basis = "mega-cap on mapped committee"
    elif mega_cap and not aligned:
        # Mega-cap override but politician's committees don't include the
        # ticker's mapped committees. v1 limitation: without committee history
        # we can't fully validate. Treat as "tangential" 0.5x.
        multiplier = 0.5
        basis = "mega-cap tangential"

    # Leadership floor (chamber-wide leadership applies a 0.7x floor on all
    # trades, raising 0.3x and 0.5x to 0.7x but not lowering 1.0x)
    if pol_leadership:
        for entry in pol_leadership:
            scope = (entry.get("scope") or "").lower()
            if "chamber-wide" in scope or "chamber" in scope:
                if multiplier < 0.7:
                    multiplier = 0.7
                    basis = f"{basis} → leadership floor 0.7x"
                    leadership_floor = True
                break

    # Committee-specific leadership (chair, ranking member) raises floor
    # WITHIN that committee's jurisdiction. Without committee history we
    # approximate: if politician has any committee leadership AND the trade
    # is in any aligned sector, apply the floor.
    if pol_leadership and aligned and not leadership_floor:
        for entry in pol_leadership:
            position = (entry.get("position") or "").lower()
            if "chair" in position or "ranking" in position:
                if multiplier < 0.7:
                    multiplier = 0.7
                    basis = f"{basis} → committee leadership floor 0.7x"
                    leadership_floor = True
                break

    return {
        "multiplier": multiplier,
        "basis": basis,
        "committee_aligned": aligned,
        "mega_cap": mega_cap,
        "leadership_floor_applied": leadership_floor,
        "politician_in_db": pol_row is not None,
        "roster_tier": pol_roster_tier,
    }


# ---------------------------------------------------------------------------
# Stage 2 (Lite) — Opportunity Window Diagnostic
# ---------------------------------------------------------------------------

def compute_owd_score(conn, trade: Dict, params: Optional[Dict] = None) -> Dict:
    """
    Stage 2 Lite from specs/03-diagnostics.md.

    Five computable checks (A1, A2, A4, B3, B4). Two MMD-dependent checks
    (A3, B1) and one web-search-dependent check (B2) are deferred — see
    skipped_checks list in the return dict.

    Returns dict with:
      - score_a, score_b, score_total
      - verdict: "open" | "narrowing" | "closed"
      - checks_fired: list of strings ("A1", "A2", ...)
      - skipped_checks: list of strings (with reason)
      - raw_metrics: full stock_metrics dict for downstream use
      - thresholds: dict of computed threshold values
      - rationale: per-check fired/not-fired with values
    """
    if params is None:
        params = load_params(conn)

    ticker = trade["ticker"]
    sector = trade.get("sector") or sectors.classify_ticker(ticker)
    trade_date = trade["trade_date"]
    txn_type = (trade.get("transaction_type") or "buy").lower()

    # Pull all yfinance-derived metrics
    metrics = stock_metrics.compute_metrics_for_trade(
        ticker=ticker,
        sector=sector,
        trade_date=trade_date,
        transaction_type=txn_type,
        k_A4_window=int(params["k_A4_window"]),
        k_A4_mult=float(params["k_A4_mult"]),
    )

    score_a = 0
    score_b = 0
    checks_fired: List[str] = []
    skipped_checks: List[str] = []
    rationale: Dict[str, Dict] = {}
    thresholds: Dict[str, Optional[float]] = {}

    # ---- Helper: directional move in trader's favor ----
    # For a buy, "favorable" = price went up (positive signed move).
    # For a sell, "favorable" = price went down (negative signed move).
    # The OWD checks A1/A2/B3 fire only when the stock moved IN the
    # trader's direction beyond a threshold — that's when the edge is
    # consumed. A buy that LOST money is actually a better entry today,
    # not a degraded one, so it should NOT fire range/sigma checks.
    actual_signed = metrics.get("actual_move_pct")
    if actual_signed is not None:
        favorable_move = actual_signed if txn_type == "buy" else -actual_signed
    else:
        favorable_move = None

    # ---- A1: Range consumption (favorable direction only) ----
    hr45 = metrics.get("hist_range_45d")
    if hr45 is not None and favorable_move is not None:
        thr_a1 = params["k_A1"] * hr45
        thresholds["a1"] = thr_a1
        # Fire only if move is in trader's favor beyond threshold
        fired = favorable_move > thr_a1
        if fired:
            score_a += 1
            checks_fired.append("A1")
        rationale["A1"] = {
            "fired": fired,
            "favorable_move_pct": round(favorable_move * 100, 2),
            "threshold_pct": round(thr_a1 * 100, 2),
            "explanation": (
                "favorable directional move > k_A1 × hist_range_45d "
                "(only fires when edge is being consumed, not when entry improved)"
            ),
        }
    else:
        skipped_checks.append("A1: missing hist_range_45d or actual_move")
        rationale["A1"] = {"fired": None, "skipped": True}

    # ---- A2: Sigma exhaustion + RSI extreme (favorable direction only) ----
    sigma = metrics.get("sigma_move_pct")
    rsi = metrics.get("rsi_14")
    if sigma is not None and rsi is not None and favorable_move is not None:
        thr_a2 = params["k_A2"] * sigma
        thresholds["a2"] = thr_a2
        # Sigma breach must be in favorable direction
        sigma_breach = favorable_move > thr_a2
        rsi_extreme = (
            (txn_type == "buy" and rsi > 70)
            or (txn_type == "sell" and rsi < 30)
        )
        fired = sigma_breach and rsi_extreme
        if fired:
            score_a += 1
            checks_fired.append("A2")
        rationale["A2"] = {
            "fired": fired,
            "favorable_move_pct": round(favorable_move * 100, 2),
            "sigma_threshold_pct": round(thr_a2 * 100, 2),
            "rsi": round(rsi, 1),
            "rsi_extreme": rsi_extreme,
            "explanation": (
                "favorable move > k_A2 × sigma_move AND RSI extreme"
            ),
        }
    else:
        skipped_checks.append("A2: missing sigma_move or RSI or actual_move")
        rationale["A2"] = {"fired": None, "skipped": True}

    # ---- A3: IV expansion (MMD-deferred in v1) ----
    skipped_checks.append("A3: MMD-deferred (Phase 2.3.5)")
    rationale["A3"] = {"fired": None, "skipped": True, "reason": "MMD only"}
    thresholds["a3"] = None

    # ---- A4: Volume front-run ----
    vol_spike = metrics.get("volume_spike")
    if vol_spike is not None:
        fired = vol_spike.get("fired", False)
        if fired:
            score_a += 1
            checks_fired.append("A4")
        rationale["A4"] = {
            "fired": fired,
            "max_volume_ratio": vol_spike.get("max_ratio"),
            "spike_dates": vol_spike.get("spike_dates"),
            "k_A4_mult": params["k_A4_mult"],
            "explanation": (
                f"any day in post-trade window had volume "
                f"> {params['k_A4_mult']}x 20d avg"
            ),
        }
    else:
        skipped_checks.append("A4: missing volume data")
        rationale["A4"] = {"fired": None, "skipped": True}

    # ---- B1: Earnings pass-through (MMD-deferred in v1) ----
    skipped_checks.append("B1: MMD-deferred (Phase 2.3.5)")
    rationale["B1"] = {"fired": None, "skipped": True, "reason": "MMD only"}
    thresholds["b1"] = None

    # ---- B2: Legislative/regulatory event (web search → handled by LLM later) ----
    # Stage 3 LLM may surface a past catalyst that fires B2 retroactively.
    # We log B2 as "deferred-to-stage3" here; the runner can re-score B2
    # post-LLM. For v1, B2 stays at 0 in the deterministic Phase A pass.
    skipped_checks.append("B2: web-search-dependent, deferred to Stage 3 LLM")
    rationale["B2"] = {"fired": None, "skipped": True, "reason": "deferred"}

    # ---- B3: News absorption ----
    # We approximate B3 as a soft check: if A2 fired (sigma breach + RSI
    # extreme), there's evidence of major price movement. The LLM Phase 3
    # may upgrade this with a real catalyst attribution; for the
    # deterministic pass, fire B3 only if A2 fired (proxy for "news + move").
    if "A2" in checks_fired:
        score_b += 1
        checks_fired.append("B3")
        rationale["B3"] = {
            "fired": True,
            "explanation": "proxy: A2 fired, indicating news absorption",
            "k_B3": params["k_B3"],
        }
    else:
        rationale["B3"] = {
            "fired": False,
            "explanation": "no A2 trigger; proxy not fired",
        }
    thresholds["b3"] = None  # not directly computed; proxy via A2

    # ---- B4: Sector rotation ----
    sector_move = metrics.get("sector_etf_move")
    sector_vol = metrics.get("sector_vol_60d")
    days_elapsed = metrics.get("sector_days_elapsed") or metrics.get("days_elapsed")
    if (
        sector_move is not None
        and sector_vol is not None
        and days_elapsed
        and days_elapsed > 0
    ):
        # Spec: > k_B4 × sector_vol_60d / √252 × √days_elapsed
        thr_b4 = (
            params["k_B4"]
            * sector_vol
            * math.sqrt(days_elapsed / 252.0)
        )
        thresholds["b4"] = thr_b4
        fired = abs(sector_move) > thr_b4
        if fired:
            score_b += 1
            checks_fired.append("B4")
        rationale["B4"] = {
            "fired": fired,
            "sector_etf": metrics.get("sector_etf"),
            "sector_move_pct": round(sector_move * 100, 2),
            "threshold_pct": round(thr_b4 * 100, 2),
            "days_elapsed": days_elapsed,
            "explanation": "sector ETF moved > k_B4 × sector_vol scaled to elapsed time",
        }
    else:
        skipped_checks.append("B4: missing sector vol or move data")
        rationale["B4"] = {"fired": None, "skipped": True}

    # ---- Final score + verdict ----
    score_total = score_a + score_b
    threshold_open = int(params["threshold_open"])
    threshold_narrowing = int(params["threshold_narrowing"])

    if score_total <= threshold_open:
        verdict = OWD_OPEN
    elif score_total <= threshold_narrowing:
        verdict = OWD_NARROWING
    else:
        verdict = OWD_CLOSED

    return {
        "score_a": score_a,
        "score_b": score_b,
        "score_total": score_total,
        "verdict": verdict,
        "checks_fired": checks_fired,
        "skipped_checks": skipped_checks,
        "rationale": rationale,
        "thresholds": thresholds,
        "raw_metrics": metrics,
    }


# ---------------------------------------------------------------------------
# Stage 4 — Clustering Check
# ---------------------------------------------------------------------------

def find_clustering(conn, trade: Dict, days: int = 30) -> Dict:
    """
    Stage 4: query for other roster members holding/buying the same ticker
    within the past `days` days. Spouse trades count at 0.5x weight.

    Returns dict with:
      - cluster_count: float (member_direct = 1.0, spouse = 0.5)
      - member_count: int
      - spouse_count: int
      - cross_party: bool (>=2 politicians from each major party)
      - bumps_tier: bool (3+ in cluster_count OR cross_party)
      - politicians: list of {name, party, trader_tag, trade_date}
      - same_sector_count: int (politicians who bought ANY ticker in the
        same sector in the window — broader cluster signal)
    """
    ticker = trade.get("ticker") or ""
    trade_date = trade.get("trade_date") or datetime.utcnow().strftime("%Y-%m-%d")
    self_id = trade.get("id")

    # Query trades in the same ticker, in window
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT politician_name, trader_tag, trade_date, transaction_type
        FROM trades
        WHERE ticker = ?
          AND trade_date >= DATE(?, ?)
          AND trade_date <= ?
          AND (id IS NULL OR id != COALESCE(?, -1))
          AND transaction_type = 'buy'
        """,
        (ticker.upper(), trade_date, f"-{days} days", trade_date, self_id),
    )
    rows = [dict(r) for r in cur.fetchall()]

    # Look up parties for each unique politician
    politicians_by_name: Dict[str, Dict] = {}
    for r in rows:
        name = r["politician_name"]
        if name not in politicians_by_name:
            politicians_by_name[name] = {
                "name": name,
                "trader_tag": r["trader_tag"],
                "trade_date": r["trade_date"],
                "party": None,
            }
            # Look up party
            try:
                pol = db.get_politician(conn, name)
                if pol:
                    politicians_by_name[name]["party"] = pol["party"]
            except Exception:
                pass

    politicians = list(politicians_by_name.values())
    member_count = sum(1 for p in politicians if p["trader_tag"] == "member_direct")
    spouse_count = sum(1 for p in politicians if p["trader_tag"] == "spouse")
    cluster_count = float(member_count) + 0.5 * float(spouse_count)

    # Cross-party check
    party_counts: Dict[str, int] = {}
    for p in politicians:
        party = (p.get("party") or "").upper()
        if party in ("D", "R"):
            party_counts[party] = party_counts.get(party, 0) + 1
    cross_party = party_counts.get("D", 0) >= 2 and party_counts.get("R", 0) >= 2

    # Same-sector cluster (broader signal — not used for tier bump in v1)
    sector = trade.get("sector")
    same_sector_count = 0
    if sector:
        cur.execute(
            """
            SELECT COUNT(DISTINCT politician_name) FROM trades
            WHERE sector = ?
              AND trade_date >= DATE(?, ?)
              AND trade_date <= ?
              AND ticker != ?
              AND transaction_type = 'buy'
            """,
            (sector, trade_date, f"-{days} days", trade_date, ticker.upper()),
        )
        row = cur.fetchone()
        if row:
            same_sector_count = row[0]

    bumps_tier = cluster_count >= 3.0 or cross_party

    return {
        "cluster_count": round(cluster_count, 2),
        "member_count": member_count,
        "spouse_count": spouse_count,
        "cross_party": cross_party,
        "bumps_tier": bumps_tier,
        "politicians": politicians,
        "same_sector_count": same_sector_count,
        "window_days": days,
    }


# ---------------------------------------------------------------------------
# Final tier assembly (after Stage 3 LLM results merged in)
# ---------------------------------------------------------------------------

def assemble_final_tier(
    stage1: Dict,
    stage2: Dict,
    stage3_catalyst: Optional[Dict],
    stage4: Dict,
) -> Dict:
    """
    Combine Stage 1 multiplier, Stage 2 verdict, Stage 3 forward-catalyst
    findings, and Stage 4 clustering into the final {STRONG, BASE, MODERATE,
    SKIP} tier. Returns {tier, downgraded_from, reason, has_catalyst,
    has_clustering}.

    stage3_catalyst: dict from LLM Phase B with at least
        {forward_catalyst: text or None, catalyst_date: ISO or None}
        OR None if Phase B hasn't run yet.

    Tier rules (per spec):
      STRONG   = 1.0x aligned + forward catalyst + clustering, window open
      BASE     = 1.0x aligned + forward catalyst, window open
      MODERATE = aligned only, some ambiguity (or forward catalyst missing)
      SKIP     = pipeline failure (window closed, dependent trade, etc.)

    Multiplier downgrades (per spec):
      - 0.5x can reach BASE max
      - 0.3x needs strong clustering for BASE
      - leadership floor 0.7x doesn't lower 1.0x but raises 0.3x/0.5x to 0.7x
    """
    multiplier = stage1["multiplier"]
    verdict = stage2["verdict"]
    score_total = stage2["score_total"]
    has_catalyst = bool(stage3_catalyst and stage3_catalyst.get("forward_catalyst"))
    has_clustering = stage4.get("bumps_tier", False)

    # ---- Hard skips ----
    if verdict == OWD_CLOSED:
        return {
            "tier": TIER_SKIP,
            "reason": f"opportunity-expired (OWD score {score_total})",
            "has_catalyst": has_catalyst,
            "has_clustering": has_clustering,
            "multiplier": multiplier,
        }

    # ---- Determine the natural (pre-multiplier) tier ----
    if verdict == OWD_OPEN and has_catalyst and has_clustering:
        natural = TIER_STRONG
    elif verdict == OWD_OPEN and has_catalyst:
        natural = TIER_BASE
    elif verdict in (OWD_OPEN, OWD_NARROWING) and has_clustering:
        natural = TIER_BASE
    elif verdict in (OWD_OPEN, OWD_NARROWING):
        natural = TIER_MODERATE
    else:
        natural = TIER_SKIP

    # ---- Apply multiplier downgrades ----
    final = natural
    downgraded_from = None

    if multiplier < 1.0 and natural == TIER_STRONG:
        final = TIER_BASE
        downgraded_from = TIER_STRONG

    if multiplier <= 0.5 and final == TIER_STRONG:
        final = TIER_BASE
        downgraded_from = TIER_STRONG

    if multiplier <= 0.5 and final == TIER_BASE and not has_clustering:
        final = TIER_MODERATE
        downgraded_from = downgraded_from or TIER_BASE

    if multiplier <= 0.3 and not has_clustering:
        final = TIER_MODERATE if final in (TIER_STRONG, TIER_BASE) else final
        if final == TIER_MODERATE and natural in (TIER_STRONG, TIER_BASE):
            downgraded_from = downgraded_from or natural

    # ---- Late-entry flag (window narrowing → trim conviction) ----
    if verdict == OWD_NARROWING and final == TIER_STRONG:
        final = TIER_BASE
        downgraded_from = downgraded_from or TIER_STRONG

    reason_parts = [f"alignment={multiplier:.1f}", f"owd={verdict}({score_total})"]
    reason_parts.append("catalyst=" + ("yes" if has_catalyst else "no"))
    reason_parts.append("clustering=" + ("yes" if has_clustering else "no"))
    if downgraded_from:
        reason_parts.append(f"downgraded_from={downgraded_from}")

    return {
        "tier": final,
        "downgraded_from": downgraded_from,
        "reason": " ".join(reason_parts),
        "natural": natural,
        "has_catalyst": has_catalyst,
        "has_clustering": has_clustering,
        "multiplier": multiplier,
    }


# ---------------------------------------------------------------------------
# Top-level orchestrator (Phase A — runs Stages 1, 2, 4)
# ---------------------------------------------------------------------------

def score_trade(conn, trade: Dict, params: Optional[Dict] = None) -> Dict:
    """
    Run Stages 1, 2, 4 on one trade. Returns merged dict with the full
    pipeline trace, ready for the trade_diagnostics table.

    Stage 3 (forward catalyst) is LLM-driven and runs separately in Phase B.
    Call assemble_final_tier() with the LLM's Stage 3 output to get the
    canonical {STRONG, BASE, MODERATE, SKIP} verdict.

    Special cases:
      - dependent trades return SKIP without running pipeline
      - trades with no ticker return SKIP
    """
    if not trade.get("ticker"):
        return {
            "trade_id": trade.get("id"),
            "tier_pre_stage3": TIER_SKIP,
            "skip_reason": "no ticker",
        }

    if trade.get("trader_tag") == "dependent":
        return {
            "trade_id": trade.get("id"),
            "tier_pre_stage3": TIER_SKIP,
            "skip_reason": "dependent trade — not scored per spec",
        }

    if params is None:
        params = load_params(conn)

    # ---- Stage 1 ----
    stage1 = compute_alignment_multiplier(conn, trade)

    # ---- Stage 2 ----
    stage2 = compute_owd_score(conn, trade, params=params)

    # ---- Stage 4 ----
    stage4 = find_clustering(conn, trade, days=int(params["k_cluster_window"]))

    # Compute a "pre-Stage-3 tier" — what the trade would score if no
    # forward catalyst is found. The runner can call assemble_final_tier()
    # again after Stage 3 to upgrade with catalyst data.
    pre_tier = assemble_final_tier(stage1, stage2, stage3_catalyst=None, stage4=stage4)

    return {
        "trade_id": trade.get("id"),
        "stage1": stage1,
        "stage2": stage2,
        "stage4": stage4,
        "tier_pre_stage3": pre_tier["tier"],
        "tier_pre_stage3_reason": pre_tier["reason"],
        "params_used": params,
    }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json
    if len(sys.argv) < 2:
        print("Usage: python3 pipeline.py SCORE_TRADE_ID")
        print("       python3 pipeline.py SCORE_LATEST_BUYS [N]")
        print("       python3 pipeline.py CLUSTER TICKER")
        sys.exit(1)

    cmd = sys.argv[1]
    conn = db.connect()

    if cmd == "SCORE_TRADE_ID" and len(sys.argv) >= 3:
        trade_id = int(sys.argv[2])
        cur = conn.cursor()
        cur.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        row = cur.fetchone()
        if not row:
            print(f"trade id {trade_id} not found")
            sys.exit(1)
        trade = dict(row)
        result = score_trade(conn, trade)
        print(_json.dumps(result, indent=2, default=str))

    elif cmd == "SCORE_LATEST_BUYS":
        n = int(sys.argv[2]) if len(sys.argv) >= 3 else 5
        cur = conn.cursor()
        cur.execute(
            """SELECT * FROM trades
               WHERE transaction_type = 'buy' AND trader_tag = 'member_direct'
               ORDER BY trade_date DESC LIMIT ?""",
            (n,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        print(f"Scoring {len(rows)} latest member-direct buys...\n")
        for r in rows:
            res = score_trade(conn, r)
            s1 = res.get("stage1", {})
            s2 = res.get("stage2", {})
            s4 = res.get("stage4", {})
            print(
                f"  {r['politician_name'][:25]:25s} {r['ticker']:5s} {r['trade_date']} "
                f"-> tier={res.get('tier_pre_stage3')} "
                f"mult={s1.get('multiplier')} owd={s2.get('verdict')}({s2.get('score_total')}) "
                f"cluster={s4.get('cluster_count')} (cross_party={s4.get('cross_party')})"
            )

    elif cmd == "CLUSTER" and len(sys.argv) >= 3:
        ticker = sys.argv[2].upper()
        cur = conn.cursor()
        cur.execute("SELECT * FROM trades WHERE ticker = ? LIMIT 1", (ticker,))
        row = cur.fetchone()
        if not row:
            print(f"no trades for ticker {ticker}")
            sys.exit(1)
        trade = dict(row)
        result = find_clustering(conn, trade, days=30)
        print(_json.dumps(result, indent=2, default=str))

    else:
        print("Unknown command")
        sys.exit(1)

    backtest.save_price_cache()
    conn.close()
