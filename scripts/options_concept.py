#!/usr/bin/env python3
"""
options_concept.py — conceptual options play generator for the Daily Signal
agent (Phase 2.3).

No MMD, no network, no live chains. Pure rules from specs/05-options.md.
For each scored trade, returns a dict describing the recommended structure
(call vs LEAPS), a delta target band, a DTE window (catalyst-driven if
present), and a one-line rationale. The LLM Phase B reads these structures
and weaves them into the daily digest narrative.

Phase 2.3.5 will replace this with a real-Greeks variant for STRONG-tier
trades that calls MMD options chains and selects specific strikes.

Usage:
    from options_concept import concept_for_trade
    concept = concept_for_trade(
        signal_tier="STRONG",
        has_catalyst=True,
        catalyst_date="2026-04-25",
        today="2026-04-08",
    )
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, Optional

# Default DTE windows when no catalyst is identified
DEFAULT_DTE_RANGES = {
    "STRONG": (42, 84),     # 6–12 weeks
    "BASE":   (42, 70),     # 6–10 weeks
    "MODERATE": (28, 56),   # 4–8 weeks
}

# Default delta bands per tier
DEFAULT_DELTA_BANDS = {
    "STRONG":   (0.40, 0.55),  # base call (slightly aggressive)
    "BASE":     (0.40, 0.50),  # base call
    "MODERATE": (0.35, 0.45),  # lighter (slightly OTM)
}

# LEAPS thresholds — if catalyst is structural / multi-quarter, use LEAPS
LEAPS_DTE_DAYS = 365
LEAPS_DELTA_BAND = (0.65, 0.80)


def _days_between(d1_iso: str, d2_iso: str) -> Optional[int]:
    """Calendar days between two ISO date strings, or None on parse failure."""
    try:
        d1 = datetime.strptime(d1_iso, "%Y-%m-%d").date()
        d2 = datetime.strptime(d2_iso, "%Y-%m-%d").date()
        return (d2 - d1).days
    except (ValueError, TypeError):
        return None


def _add_days(iso_date: str, days: int) -> str:
    d = datetime.strptime(iso_date, "%Y-%m-%d").date()
    return (d + timedelta(days=days)).strftime("%Y-%m-%d")


def concept_for_trade(
    signal_tier: str,
    has_catalyst: bool,
    catalyst_date: Optional[str] = None,
    catalyst_label: Optional[str] = None,
    today: Optional[str] = None,
    structural_thesis: bool = False,
    transaction_type: str = "buy",
) -> Dict:
    """
    Generate a conceptual options play for a scored trade.

    Args:
        signal_tier: "STRONG" | "BASE" | "MODERATE" (anything else returns None play)
        has_catalyst: did Stage 3 LLM find a forward catalyst in the 90-day window?
        catalyst_date: ISO date of the identified catalyst (if any)
        catalyst_label: short text label for the catalyst (e.g., "EQT earnings", "FDA PDUFA")
        today: ISO date for "as of"; defaults to UTC today
        structural_thesis: True if this is a multi-quarter / multi-year thesis
                           that warrants LEAPS instead of front-month
        transaction_type: "buy" → call structure; "sell" → put structure
                          (the politician's direction guides ours)

    Returns:
        dict with:
            structure: "call" | "put" | "LEAPS call" | "LEAPS put" | "no play"
            direction: "bullish" | "bearish"
            delta_target: "0.40–0.55" (string range)
            dte_window: human-readable string ("until ~May 9, 2026 (32d)" or "6–10 weeks")
            target_expiry_iso: ISO date or None
            min_dte: int or None
            max_dte: int or None
            rationale: one-sentence explanation
            iv_warning: None (Phase 2.3.5 fills this when MMD is wired)
            tier_note: optional note about why this tier
    """
    if today is None:
        today = datetime.utcnow().strftime("%Y-%m-%d")

    direction = "bullish" if transaction_type.lower() == "buy" else "bearish"
    side = "call" if direction == "bullish" else "put"

    # ---- MODERATE: no specific play recommended ----
    if signal_tier == "MODERATE":
        return {
            "structure": "no play",
            "direction": direction,
            "delta_target": None,
            "dte_window": None,
            "target_expiry_iso": None,
            "min_dte": None,
            "max_dte": None,
            "rationale": (
                "MODERATE tier — pipeline produced ambiguity. No specific "
                "options structure recommended; track but do not enter."
            ),
            "iv_warning": None,
            "tier_note": "MODERATE",
        }

    # ---- SKIP / unknown: no play ----
    if signal_tier not in ("STRONG", "BASE"):
        return {
            "structure": "no play",
            "direction": direction,
            "delta_target": None,
            "dte_window": None,
            "target_expiry_iso": None,
            "min_dte": None,
            "max_dte": None,
            "rationale": f"signal tier '{signal_tier}' is not actionable",
            "iv_warning": None,
            "tier_note": signal_tier,
        }

    # ---- LEAPS branch: multi-quarter structural thesis ----
    if structural_thesis:
        target_expiry = _add_days(today, LEAPS_DTE_DAYS)
        return {
            "structure": f"LEAPS {side}",
            "direction": direction,
            "delta_target": f"{LEAPS_DELTA_BAND[0]:.2f}–{LEAPS_DELTA_BAND[1]:.2f}",
            "dte_window": "12–18 months",
            "target_expiry_iso": target_expiry,
            "min_dte": LEAPS_DTE_DAYS,
            "max_dte": LEAPS_DTE_DAYS + 180,
            "rationale": (
                "Multi-quarter thesis — LEAPS captures structural move "
                "without weekly theta drag. Higher delta provides more "
                "stock-like exposure."
            ),
            "iv_warning": None,
            "tier_note": signal_tier,
        }

    # ---- Catalyst-driven branch: DTE = catalyst_date + 14d buffer ----
    if has_catalyst and catalyst_date:
        days_to_catalyst = _days_between(today, catalyst_date)
        if days_to_catalyst is None or days_to_catalyst < 0:
            # Catalyst date is in the past or unparseable — fall through to default
            has_catalyst = False
        elif days_to_catalyst > 270:
            # Catalyst is too far out for a front-month structure — use LEAPS
            target_expiry = _add_days(today, LEAPS_DTE_DAYS)
            return {
                "structure": f"LEAPS {side}",
                "direction": direction,
                "delta_target": f"{LEAPS_DELTA_BAND[0]:.2f}–{LEAPS_DELTA_BAND[1]:.2f}",
                "dte_window": "12–18 months",
                "target_expiry_iso": target_expiry,
                "min_dte": LEAPS_DTE_DAYS,
                "max_dte": LEAPS_DTE_DAYS + 180,
                "rationale": (
                    f"Catalyst ({catalyst_label or 'identified event'}) is "
                    f"~{days_to_catalyst} days out — LEAPS gives runway "
                    f"without theta drag."
                ),
                "iv_warning": None,
                "tier_note": signal_tier,
            }
        else:
            # Standard catalyst-driven DTE: catalyst + 2-week buffer
            target_dte = days_to_catalyst + 14
            min_dte = max(target_dte - 7, 14)  # never less than 14 DTE
            max_dte = target_dte + 14
            target_expiry = _add_days(today, target_dte)
            band = DEFAULT_DELTA_BANDS[signal_tier]
            return {
                "structure": side,
                "direction": direction,
                "delta_target": f"{band[0]:.2f}–{band[1]:.2f}",
                "dte_window": (
                    f"~{target_dte}d (target expiry {target_expiry}, "
                    f"covers {catalyst_label or 'catalyst'} "
                    f"on {catalyst_date} + 14d buffer)"
                ),
                "target_expiry_iso": target_expiry,
                "min_dte": min_dte,
                "max_dte": max_dte,
                "rationale": (
                    f"DTE selected to cover the {catalyst_label or 'forward catalyst'} "
                    f"on {catalyst_date} with a 14-day buffer for delayed reaction."
                ),
                "iv_warning": None,
                "tier_note": signal_tier,
            }

    # ---- Default: no catalyst, use tier defaults ----
    default_min, default_max = DEFAULT_DTE_RANGES[signal_tier]
    band = DEFAULT_DELTA_BANDS[signal_tier]
    weeks_min = default_min // 7
    weeks_max = default_max // 7
    return {
        "structure": side,
        "direction": direction,
        "delta_target": f"{band[0]:.2f}–{band[1]:.2f}",
        "dte_window": f"{weeks_min}–{weeks_max} weeks",
        "target_expiry_iso": _add_days(today, (default_min + default_max) // 2),
        "min_dte": default_min,
        "max_dte": default_max,
        "rationale": (
            f"No specific forward catalyst identified — default {weeks_min}–"
            f"{weeks_max} week DTE for {signal_tier.lower()}-tier conviction."
        ),
        "iv_warning": None,
        "tier_note": signal_tier,
    }


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json
    cases = [
        ("STRONG with near catalyst", {
            "signal_tier": "STRONG",
            "has_catalyst": True,
            "catalyst_date": "2026-04-25",
            "catalyst_label": "EQT Q1 earnings",
            "today": "2026-04-08",
        }),
        ("STRONG no catalyst", {
            "signal_tier": "STRONG",
            "has_catalyst": False,
            "today": "2026-04-08",
        }),
        ("STRONG structural multi-quarter", {
            "signal_tier": "STRONG",
            "has_catalyst": True,
            "catalyst_date": "2027-01-15",
            "catalyst_label": "AI cloud capex cycle",
            "today": "2026-04-08",
            "structural_thesis": True,
        }),
        ("BASE with catalyst", {
            "signal_tier": "BASE",
            "has_catalyst": True,
            "catalyst_date": "2026-05-12",
            "catalyst_label": "FERC ruling",
            "today": "2026-04-08",
        }),
        ("BASE no catalyst", {
            "signal_tier": "BASE",
            "has_catalyst": False,
            "today": "2026-04-08",
        }),
        ("MODERATE", {
            "signal_tier": "MODERATE",
            "has_catalyst": False,
            "today": "2026-04-08",
        }),
        ("STRONG sell direction", {
            "signal_tier": "STRONG",
            "has_catalyst": True,
            "catalyst_date": "2026-04-25",
            "catalyst_label": "earnings",
            "today": "2026-04-08",
            "transaction_type": "sell",
        }),
    ]
    for label, kwargs in cases:
        concept = concept_for_trade(**kwargs)
        print(f"\n=== {label} ===")
        print(_json.dumps(concept, indent=2))
