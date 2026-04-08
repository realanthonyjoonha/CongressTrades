#!/usr/bin/env python3
"""
options_chain.py — Real options-chain fetching + strike selection.
Phase 2.3.5.

Primary data source: yfinance `Ticker.option_chain()`. This returns the
full chain for a given expiry with columns: contractSymbol, strike, bid,
ask, lastPrice, volume, openInterest, **impliedVolatility**.

From that IV, we compute delta/gamma/theta/vega per strike via
Black-Scholes (see `bs_greeks.py`), then filter to the target delta
band and DTE window specified by the signal tier.

Why yfinance and not MMD?
  - MMD's options snapshot endpoint requires a higher subscription tier.
    Reference data + aggregate bars work but don't include live IV.
  - yfinance returns the full chain with IV in one network call.
  - Free, no subscription negotiation, works in Dispatch sandboxes.
  - A thin MMD client (scripts/mmd_client.py) is built alongside for
    future historical backtesting via options aggregate bars.

Graceful degradation:
  - Off-hours: bid/ask can be stale; IV may be zero for some strikes.
    Filter those out before BS computation.
  - yfinance network failure: fall back to conceptual options (the
    old `options_concept.py` behavior).
  - Ticker has no listed options: same fallback.

Usage:
    from options_chain import fetch_chain, best_strike_for_trade
    result = best_strike_for_trade(
        ticker="NVDA",
        signal_tier="STRONG",
        has_catalyst=True,
        catalyst_date="2026-05-20",
        today="2026-04-08",
    )
    # result: {'strike': 180.0, 'delta': 0.45, 'dte': 42, 'expiry': '2026-05-20', ...}
    # OR falls back to conceptual on failure.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bs_greeks  # noqa: E402
import options_concept  # noqa: E402

# Target delta bands per tier + optional aggressive sub-tier
DELTA_BANDS: Dict[str, Tuple[float, float]] = {
    "STRONG_BASE": (0.40, 0.55),    # base call, moderately aggressive
    "STRONG_AGG": (0.25, 0.35),     # aggressive OTM call
    "STRONG_LEAPS": (0.65, 0.80),   # ITM LEAPS
    "BASE": (0.40, 0.50),
    "BASE_AGG": (0.30, 0.40),
}

# Central delta target per tier (what pick_strike_by_delta aims for)
TARGET_DELTAS: Dict[str, float] = {
    "STRONG": 0.45,
    "BASE": 0.45,
    "MODERATE": None,  # no play recommended
    "STRONG_LEAPS": 0.72,
}

# DTE parameters
LEAPS_DTE_DAYS = 365
DEFAULT_NO_CATALYST_DTE = (42, 84)  # 6-12 weeks
CATALYST_BUFFER_DAYS = 14


# ---------------------------------------------------------------------------
# Chain fetching
# ---------------------------------------------------------------------------

def list_expirations(ticker: str) -> List[str]:
    """Return the list of available option expiration dates for a ticker."""
    if not HAS_YF:
        return []
    try:
        t = yf.Ticker(ticker)
        exps = t.options
        return list(exps) if exps else []
    except Exception as e:
        print(f"  [options_chain] list_expirations({ticker}) failed: {e}",
              file=sys.stderr)
        return []


def fetch_chain(
    ticker: str,
    expiration: str,
    option_type: str = "call",
    spot: Optional[float] = None,
    risk_free_rate: float = bs_greeks.DEFAULT_RISK_FREE_RATE,
) -> List[Dict]:
    """
    Fetch the options chain for a specific ticker + expiration.

    Returns list of dicts per strike with:
      {strike, bid, ask, mid, lastPrice, volume, openInterest, iv,
       contractSymbol, liquidity_score}

    Critical: yfinance's `impliedVolatility` field returns garbage during
    off-hours (powers-of-2 placeholders like 0.0625, 0.125, 0.25). We
    IGNORE yfinance's IV entirely and compute it ourselves via inverse
    Black-Scholes from `lastPrice`, which is reliable across all hours.

    Sanity filters (in order):
      1. lastPrice > 0 — contract has actually traded
      2. volume > 0 OR openInterest > 0 — real-world demand exists
         (yfinance sometimes returns OI=0 for everything; we still
         require volume > 0 in that case)
      3. Spot is provided (required for inverse BS)
      4. Computed IV must be in [0.10, 2.0] — real single-stock IV
      5. Theoretical price within 20% of lastPrice (consistency check)

    An empty result means yfinance had no usable data OR every strike
    failed the sanity filters. Caller should fall back to conceptual.
    """
    if not HAS_YF:
        return []
    if spot is None or spot <= 0:
        return []

    try:
        t = yf.Ticker(ticker)
        chain = t.option_chain(expiration)
    except Exception as e:
        print(f"  [options_chain] fetch_chain({ticker}, {expiration}) failed: {e}",
              file=sys.stderr)
        return []

    df = chain.calls if option_type == "call" else chain.puts
    if df is None or df.empty:
        return []

    import math

    def _safe_float(v, default=0.0):
        try:
            fv = float(v)
            return default if math.isnan(fv) else fv
        except (TypeError, ValueError):
            return default

    # Compute days to expiry for inverse BS
    try:
        today_dt = datetime.utcnow().date()
        exp_dt = datetime.strptime(expiration, "%Y-%m-%d").date()
        dte_days = (exp_dt - today_dt).days
    except ValueError:
        return []
    if dte_days <= 0:
        return []
    T = dte_days / 365.0

    raw: List[Dict] = []
    for _, row in df.iterrows():
        strike = _safe_float(row.get("strike", 0))
        last = _safe_float(row.get("lastPrice", 0))
        if strike <= 0 or last <= 0:
            continue

        bid = _safe_float(row.get("bid", 0))
        ask = _safe_float(row.get("ask", 0))
        volume = int(_safe_float(row.get("volume", 0)))
        open_interest = int(_safe_float(row.get("openInterest", 0)))

        # Need at least some demand signal
        if volume == 0 and open_interest == 0:
            continue

        # Inverse BS to compute IV from lastPrice
        computed_iv = bs_greeks.implied_vol_from_price(
            target_price=last,
            spot=spot,
            strike=strike,
            T=T,
            r=risk_free_rate,
            option_type=option_type,
        )

        # IV sanity filter
        if computed_iv < 0.10 or computed_iv > 2.0:
            continue

        # Consistency check: BS price at computed IV should match lastPrice
        bs_p = bs_greeks.bs_price(spot, strike, T, risk_free_rate, computed_iv, option_type)
        if abs(bs_p - last) > 0.20 * max(last, 1.0):
            continue

        # Reasonable spread check (only when both bid/ask are real)
        if bid > 0 and ask > 0:
            if ask > bid * 5 and (ask - bid) > 1.0:
                continue

        mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else last
        liquidity_score = volume + (open_interest / 10.0)

        raw.append({
            "strike": strike,
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "lastPrice": last,
            "volume": volume,
            "openInterest": open_interest,
            "iv": computed_iv,  # computed via inverse BS, not yfinance's bogus value
            "liquidity_score": liquidity_score,
            "contractSymbol": str(row.get("contractSymbol", "")),
        })

    return raw


def get_spot_price(ticker: str) -> Optional[float]:
    """
    Get the current (or most-recent-close) spot price for a ticker.
    Reuses backtest's persistent price cache so repeated lookups are free.
    """
    # Import lazily to avoid circular dependency warnings
    import backtest
    # Use latest available price (same pattern as stock_metrics)
    cache = backtest._price_cache.get(ticker, {})
    if not cache:
        backtest.get_close(ticker, datetime.utcnow().strftime("%Y-%m-%d"))
        cache = backtest._price_cache.get(ticker, {})
    if not cache:
        return None
    sorted_dates = sorted(cache.keys())
    return cache[sorted_dates[-1]] if sorted_dates else None


# ---------------------------------------------------------------------------
# Expiration selection
# ---------------------------------------------------------------------------

def pick_expiration(
    expirations: List[str],
    target_dte_min: int,
    target_dte_max: int,
    today: Optional[str] = None,
) -> Optional[str]:
    """
    Given a list of available expirations and a DTE window, pick the
    best expiry. Returns the ISO date string or None.

    Strategy: first try to find an expiry inside the window. If none,
    pick the first expiry AFTER target_dte_min (i.e., lean longer-dated
    rather than shorter to avoid expiry risk).
    """
    if today is None:
        today = datetime.utcnow().strftime("%Y-%m-%d")

    today_dt = datetime.strptime(today, "%Y-%m-%d").date()

    candidates = []
    for exp in expirations:
        try:
            exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
            dte = (exp_dt - today_dt).days
            candidates.append((exp, dte))
        except ValueError:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[1])

    # 1. Prefer an expiry inside the window
    in_window = [(e, d) for e, d in candidates if target_dte_min <= d <= target_dte_max]
    if in_window:
        # Pick the middle of the window
        in_window.sort(key=lambda c: abs(c[1] - (target_dte_min + target_dte_max) / 2))
        return in_window[0][0]

    # 2. Otherwise pick the first expiry AFTER target_dte_min
    longer = [(e, d) for e, d in candidates if d >= target_dte_min]
    if longer:
        return longer[0][0]

    # 3. Fallback: the longest-dated expiry available
    return candidates[-1][0]


# ---------------------------------------------------------------------------
# DTE target derivation (from signal tier + catalyst)
# ---------------------------------------------------------------------------

def target_dte_range(
    signal_tier: str,
    has_catalyst: bool,
    catalyst_date: Optional[str] = None,
    today: Optional[str] = None,
    structural: bool = False,
) -> Tuple[int, int]:
    """
    Derive the target DTE min/max window for a trade.

    Rules from specs/05-options.md:
    - Catalyst present → DTE covers catalyst_date + 14-day buffer
    - Structural / multi-quarter → LEAPS, ~12 months
    - No catalyst → tier default (6-12 weeks)
    """
    if today is None:
        today = datetime.utcnow().strftime("%Y-%m-%d")

    if structural:
        return (LEAPS_DTE_DAYS, LEAPS_DTE_DAYS + 180)

    if has_catalyst and catalyst_date:
        try:
            today_dt = datetime.strptime(today, "%Y-%m-%d").date()
            cat_dt = datetime.strptime(catalyst_date, "%Y-%m-%d").date()
            days_to_cat = (cat_dt - today_dt).days
        except ValueError:
            days_to_cat = None

        if days_to_cat is not None and days_to_cat >= 0:
            if days_to_cat > 270:
                return (LEAPS_DTE_DAYS, LEAPS_DTE_DAYS + 180)
            target = days_to_cat + CATALYST_BUFFER_DAYS
            return (max(14, target - 7), target + 14)

    # Default tier defaults
    return DEFAULT_NO_CATALYST_DTE


# ---------------------------------------------------------------------------
# Main entry point: select the best strike for a scored trade
# ---------------------------------------------------------------------------

def best_strike_for_trade(
    ticker: str,
    signal_tier: str,
    has_catalyst: bool = False,
    catalyst_date: Optional[str] = None,
    today: Optional[str] = None,
    transaction_type: str = "buy",
    structural: bool = False,
    risk_free_rate: float = bs_greeks.DEFAULT_RISK_FREE_RATE,
) -> Dict:
    """
    Orchestrator: given a ticker + signal tier + catalyst context, return
    the best strike with real Greeks. Falls back to a conceptual play if
    the live chain is unavailable.

    Returns a dict with these fields:
      - mode: "real" or "conceptual"
      - structure: "call" / "put" / "LEAPS call" / "no play"
      - (if real) strike, expiry, dte, delta, gamma, theta, vega, iv,
                  bid, ask, mid, spot, contractSymbol
      - (if conceptual) delta_target, dte_window (from options_concept.py)
      - rationale: explanation string
      - bear_case_hint: None (LLM fills this in Phase B)
      - snapshot_caveat: timestamp string for real-strike plays
    """
    if today is None:
        today = datetime.utcnow().strftime("%Y-%m-%d")

    # MODERATE tier gets no play
    if signal_tier == "MODERATE":
        return options_concept.concept_for_trade(
            signal_tier="MODERATE",
            has_catalyst=has_catalyst,
            catalyst_date=catalyst_date,
            today=today,
            transaction_type=transaction_type,
        )

    if signal_tier not in ("STRONG", "BASE"):
        # SKIP or unknown — no play
        return {
            "mode": "conceptual",
            "structure": "no play",
            "rationale": f"tier '{signal_tier}' is not actionable",
        }

    # ---- Derive target DTE window ----
    dte_min, dte_max = target_dte_range(
        signal_tier=signal_tier,
        has_catalyst=has_catalyst,
        catalyst_date=catalyst_date,
        today=today,
        structural=structural,
    )

    # ---- Try real chain path ----
    option_type = "call" if transaction_type == "buy" else "put"

    spot = get_spot_price(ticker)
    if spot is None or spot <= 0:
        # No spot → can't compute Greeks
        return _fallback_conceptual(
            signal_tier, has_catalyst, catalyst_date, today,
            transaction_type, structural,
            reason="no spot price available",
        )

    expirations = list_expirations(ticker)
    if not expirations:
        return _fallback_conceptual(
            signal_tier, has_catalyst, catalyst_date, today,
            transaction_type, structural,
            reason="no listed options for ticker",
        )

    best_expiry = pick_expiration(expirations, dte_min, dte_max, today=today)
    if not best_expiry:
        return _fallback_conceptual(
            signal_tier, has_catalyst, catalyst_date, today,
            transaction_type, structural,
            reason="no expiration in target DTE window",
        )

    chain = fetch_chain(
        ticker, best_expiry,
        option_type=option_type,
        spot=spot,
        risk_free_rate=risk_free_rate,
    )
    if not chain:
        return _fallback_conceptual(
            signal_tier, has_catalyst, catalyst_date, today,
            transaction_type, structural,
            reason="empty chain after IV/liquidity filters",
        )

    # ---- Compute DTE and target delta ----
    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d").date()
        exp_dt = datetime.strptime(best_expiry, "%Y-%m-%d").date()
        dte = (exp_dt - today_dt).days
    except ValueError:
        dte = 30

    T = max(1, dte) / 365.0

    # Target delta depends on tier AND structural/LEAPS flag
    if structural or dte > 270:
        target_delta = 0.72  # LEAPS
    else:
        target_delta = TARGET_DELTAS.get(signal_tier, 0.45)

    # ---- Constrain strikes to a reasonable band around spot ----
    # Real-world delta-targeted strikes for ATM-ish calls are usually
    # within +/- 30% of spot. Strikes well outside that band almost
    # always indicate yfinance returning bogus IV for an illiquid
    # contract; the BS computation then accidentally matches the target
    # delta because of the inflated vol.
    if structural or dte > 270:
        # LEAPS can range further (especially deep ITM)
        spot_min = spot * 0.40
        spot_max = spot * 1.60
    else:
        spot_min = spot * 0.70
        spot_max = spot * 1.30
    chain = [c for c in chain if spot_min <= c["strike"] <= spot_max]
    if not chain:
        return _fallback_conceptual(
            signal_tier, has_catalyst, catalyst_date, today,
            transaction_type, structural,
            reason=f"no strikes within {spot_min:.0f}-{spot_max:.0f} band around spot ${spot:.2f}",
        )

    # Sort by liquidity descending so the picker's tiebreak prefers
    # liquid strikes when multiple are equally close to target_delta
    chain.sort(key=lambda c: -c["liquidity_score"])

    # ---- Pick the best strike ----
    strikes = [c["strike"] for c in chain]
    iv_map = {c["strike"]: c["iv"] for c in chain}
    picked = bs_greeks.pick_strike_by_delta(
        strikes=strikes,
        spot=spot,
        T=T,
        r=risk_free_rate,
        iv_map=iv_map,
        target_delta=target_delta,
        option_type=option_type,
    )
    if not picked:
        return _fallback_conceptual(
            signal_tier, has_catalyst, catalyst_date, today,
            transaction_type, structural,
            reason="strike picker failed to find a match",
        )

    # Enrich with quote data from the chain
    strike = picked["strike"]
    quote = next((c for c in chain if c["strike"] == strike), {})

    structure = "call" if option_type == "call" else "put"
    if dte > 270:
        structure = f"LEAPS {structure}"

    # Build the rationale
    if has_catalyst and catalyst_date and not structural:
        rationale = (
            f"Real-chain pick: {structure} strike {strike}, expiry "
            f"{best_expiry} ({dte} DTE, covers catalyst on "
            f"{catalyst_date} + {CATALYST_BUFFER_DAYS}d buffer). "
            f"Delta {picked['delta']:.2f}, IV {picked['iv']*100:.1f}%."
        )
    elif structural or dte > 270:
        rationale = (
            f"LEAPS pick: ITM {structure} strike {strike}, expiry "
            f"{best_expiry} ({dte} DTE). Delta {picked['delta']:.2f}, "
            f"IV {picked['iv']*100:.1f}%. Runway for a multi-quarter thesis."
        )
    else:
        rationale = (
            f"Real-chain pick (default DTE): {structure} strike {strike}, "
            f"expiry {best_expiry} ({dte} DTE). Delta {picked['delta']:.2f}, "
            f"IV {picked['iv']*100:.1f}%."
        )

    # Flag expensive IV
    iv_warning = None
    if picked["iv"] > 0.70:
        iv_warning = (
            f"Elevated IV ({picked['iv']*100:.1f}%) — consider a debit "
            f"spread alternative to reduce vega exposure."
        )

    snapshot_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return {
        "mode": "real",
        "structure": structure,
        "direction": "bullish" if option_type == "call" else "bearish",
        "ticker": ticker,
        "spot": spot,
        "strike": strike,
        "expiry": best_expiry,
        "dte": dte,
        "delta": picked["delta"],
        "gamma": picked["gamma"],
        "theta": picked["theta"],
        "vega": picked["vega"],
        "iv": picked["iv"],
        "theoretical_price": picked["price"],
        "bid": quote.get("bid", 0),
        "ask": quote.get("ask", 0),
        "mid": quote.get("mid", 0),
        "lastPrice": quote.get("lastPrice", 0),
        "volume": quote.get("volume", 0),
        "openInterest": quote.get("openInterest", 0),
        "contractSymbol": quote.get("contractSymbol", ""),
        "target_delta": target_delta,
        "delta_error": picked["error"],
        "rationale": rationale,
        "iv_warning": iv_warning,
        "snapshot_caveat": f"Snapshot at {snapshot_ts} — verify bid/ask before entry.",
        "tier_note": signal_tier,
    }


def _fallback_conceptual(
    signal_tier: str,
    has_catalyst: bool,
    catalyst_date: Optional[str],
    today: str,
    transaction_type: str,
    structural: bool,
    reason: str,
) -> Dict:
    """Return the old conceptual options structure, tagged as fallback."""
    concept = options_concept.concept_for_trade(
        signal_tier=signal_tier,
        has_catalyst=has_catalyst,
        catalyst_date=catalyst_date,
        today=today,
        structural_thesis=structural,
        transaction_type=transaction_type,
    )
    concept["mode"] = "conceptual"
    concept["fallback_reason"] = reason
    return concept


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json

    cases = [
        ("NVDA STRONG with catalyst", dict(
            ticker="NVDA",
            signal_tier="STRONG",
            has_catalyst=True,
            catalyst_date="2026-05-20",
            today="2026-04-08",
        )),
        ("MSFT STRONG no catalyst", dict(
            ticker="MSFT",
            signal_tier="STRONG",
            has_catalyst=False,
            today="2026-04-08",
        )),
        ("AAPL BASE with catalyst", dict(
            ticker="AAPL",
            signal_tier="BASE",
            has_catalyst=True,
            catalyst_date="2026-04-30",
            today="2026-04-08",
        )),
        ("GOOGL MODERATE", dict(
            ticker="GOOGL",
            signal_tier="MODERATE",
            has_catalyst=False,
            today="2026-04-08",
        )),
        ("XYZABC (invalid ticker, fallback test)", dict(
            ticker="XYZABC",
            signal_tier="STRONG",
            has_catalyst=False,
            today="2026-04-08",
        )),
    ]

    for label, kwargs in cases:
        print(f"\n=== {label} ===")
        result = best_strike_for_trade(**kwargs)
        # Round floats for readable output
        def _r(v):
            if isinstance(v, float):
                return round(v, 4)
            return v
        print(_json.dumps({k: _r(v) for k, v in result.items()}, indent=2, default=str))

    # Save the price cache
    import backtest
    backtest.save_price_cache()
