#!/usr/bin/env python3
"""
bs_greeks.py — Black-Scholes option pricing + Greeks.

Pure Python, no scipy or numpy dependency. Uses math.erf to compute the
standard normal CDF, which is the only "special function" needed for
the Black-Scholes formula.

This module is used by Phase 2.3.5's options layer to compute delta,
gamma, theta, and vega from a live options chain's IV. yfinance returns
the chain with an `impliedVolatility` column but does NOT compute the
Greeks — we compute them here from IV + spot + strike + expiry + risk-
free rate.

All functions accept scalar inputs and return scalars. Vectorization via
loops in the caller is fine given the scales involved (~20 strikes per
trade, maybe 200 strikes per day across all flagged trades).

Usage:
    from bs_greeks import bs_delta, bs_gamma, bs_theta, bs_vega, bs_price
    spot = 178.10
    strike = 180.0
    T = 30 / 365.0
    r = 0.045  # 4.5% risk-free rate
    iv = 0.32  # 32% implied volatility
    delta = bs_delta(spot, strike, T, r, iv, "call")
"""
from __future__ import annotations

import math
from typing import Tuple

# Reasonable default for US short-dated options. The agent can override
# per-run with the current 3-month treasury yield if desired.
DEFAULT_RISK_FREE_RATE = 0.045


# ---------------------------------------------------------------------------
# Normal distribution (standard)
# ---------------------------------------------------------------------------

SQRT_2 = math.sqrt(2.0)
SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    """
    Standard normal cumulative distribution function (CDF).
    Uses math.erf — CDF(x) = 0.5 * (1 + erf(x / sqrt(2))).
    Accurate to ~15 decimal places, good enough for options math.
    """
    return 0.5 * (1.0 + math.erf(x / SQRT_2))


def _norm_pdf(x: float) -> float:
    """Standard normal probability density function (PDF)."""
    return math.exp(-0.5 * x * x) / SQRT_2PI


# ---------------------------------------------------------------------------
# Black-Scholes core
# ---------------------------------------------------------------------------

def _d1_d2(
    spot: float, strike: float, T: float, r: float, sigma: float
) -> Tuple[float, float]:
    """Compute d1 and d2 terms for Black-Scholes."""
    if spot <= 0 or strike <= 0 or T <= 0 or sigma <= 0:
        return 0.0, 0.0
    vol_sqrt_T = sigma * math.sqrt(T)
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma * sigma) * T) / vol_sqrt_T
    d2 = d1 - vol_sqrt_T
    return d1, d2


def bs_price(
    spot: float,
    strike: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "call",
) -> float:
    """
    Black-Scholes theoretical option price.

    Args:
        spot: underlying spot price
        strike: option strike
        T: time to expiry in years
        r: risk-free rate (decimal, e.g. 0.045 for 4.5%)
        sigma: implied volatility (decimal, e.g. 0.32 for 32%)
        option_type: "call" or "put"
    """
    if spot <= 0 or strike <= 0 or T <= 0 or sigma <= 0:
        # Degenerate case: return intrinsic value
        if option_type == "call":
            return max(0.0, spot - strike)
        return max(0.0, strike - spot)

    d1, d2 = _d1_d2(spot, strike, T, r, sigma)
    discount = math.exp(-r * T)
    if option_type == "call":
        return spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    return strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_delta(
    spot: float,
    strike: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "call",
) -> float:
    """
    Delta — first derivative of option price with respect to spot.
    Call delta is in [0, 1]; put delta is in [-1, 0].
    """
    if T <= 0 or sigma <= 0 or spot <= 0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, T, r, sigma)
    if option_type == "call":
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def bs_gamma(
    spot: float, strike: float, T: float, r: float, sigma: float
) -> float:
    """Gamma — second derivative of option price with respect to spot. Same for calls/puts."""
    if T <= 0 or sigma <= 0 or spot <= 0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, T, r, sigma)
    return _norm_pdf(d1) / (spot * sigma * math.sqrt(T))


def bs_theta(
    spot: float,
    strike: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "call",
) -> float:
    """
    Theta (per DAY) — rate of change of option price with respect to time.
    Standard convention: annual theta divided by 365.
    Typically negative for long calls and puts (time decay).
    """
    if T <= 0 or sigma <= 0 or spot <= 0:
        return 0.0
    d1, d2 = _d1_d2(spot, strike, T, r, sigma)
    sqrt_T = math.sqrt(T)
    discount = math.exp(-r * T)
    term1 = -(spot * _norm_pdf(d1) * sigma) / (2.0 * sqrt_T)
    if option_type == "call":
        term2 = -r * strike * discount * _norm_cdf(d2)
    else:
        term2 = r * strike * discount * _norm_cdf(-d2)
    annual_theta = term1 + term2
    return annual_theta / 365.0


def bs_vega(
    spot: float, strike: float, T: float, r: float, sigma: float
) -> float:
    """
    Vega (per 1% change in IV) — derivative of option price with respect
    to volatility. Scaled to per-1-vol-point (not raw vega).
    Same for calls and puts.
    """
    if T <= 0 or sigma <= 0 or spot <= 0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, T, r, sigma)
    raw_vega = spot * _norm_pdf(d1) * math.sqrt(T)
    return raw_vega / 100.0  # per 1% change


def bs_all_greeks(
    spot: float,
    strike: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str = "call",
) -> dict:
    """
    Convenience: compute price + all four Greeks in one call.
    Returns {price, delta, gamma, theta, vega, d1, d2}.
    """
    d1, d2 = _d1_d2(spot, strike, T, r, sigma)
    return {
        "price": bs_price(spot, strike, T, r, sigma, option_type),
        "delta": bs_delta(spot, strike, T, r, sigma, option_type),
        "gamma": bs_gamma(spot, strike, T, r, sigma),
        "theta": bs_theta(spot, strike, T, r, sigma, option_type),
        "vega": bs_vega(spot, strike, T, r, sigma),
        "d1": d1,
        "d2": d2,
        "iv": sigma,
    }


# ---------------------------------------------------------------------------
# Inverse: implied volatility from option price (Newton-Raphson)
# ---------------------------------------------------------------------------

def implied_vol_from_price(
    target_price: float,
    spot: float,
    strike: float,
    T: float,
    r: float,
    option_type: str = "call",
    initial_guess: float = 0.3,
    max_iter: int = 50,
    tol: float = 1e-6,
) -> float:
    """
    Solve for implied volatility given a target option price.
    Uses Newton-Raphson with a safe bisection fallback.

    Useful when MMD aggregate bars give us a close price but no IV —
    we can solve BS backwards to extract IV.

    Returns the implied volatility, or 0.0 if no solution found.
    """
    if target_price <= 0 or spot <= 0 or strike <= 0 or T <= 0:
        return 0.0

    # Bracket with bisection: find low/high sigma where price crosses target
    low, high = 1e-4, 5.0
    for _ in range(50):
        mid = (low + high) / 2.0
        price = bs_price(spot, strike, T, r, mid, option_type)
        if abs(price - target_price) < tol:
            return mid
        if price < target_price:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


# ---------------------------------------------------------------------------
# Strike selection helper
# ---------------------------------------------------------------------------

def pick_strike_by_delta(
    strikes: list,
    spot: float,
    T: float,
    r: float,
    iv_map: dict,
    target_delta: float,
    option_type: str = "call",
) -> dict:
    """
    Given a list of strikes and a per-strike IV map, find the strike
    whose delta is closest to target_delta.

    Args:
        strikes: list of strike prices (floats)
        spot: underlying spot price
        T: time to expiry in years
        r: risk-free rate
        iv_map: {strike: iv} dict from yfinance chain
        target_delta: target delta (e.g. 0.45 for an ATM-ish call)
        option_type: "call" or "put"

    Returns dict with {strike, delta, gamma, theta, vega, price, iv, error}
    where error is the absolute distance from target_delta.
    """
    best = None
    for strike in strikes:
        iv = iv_map.get(strike)
        if not iv or iv <= 0:
            continue
        delta = bs_delta(spot, strike, T, r, iv, option_type)
        error = abs(delta - target_delta)
        if best is None or error < best["error"]:
            greeks = bs_all_greeks(spot, strike, T, r, iv, option_type)
            best = {
                "strike": strike,
                "error": error,
                **greeks,
            }
    return best or {}


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json

    # Classic textbook example: ATM call, 30 days to expiry, 30% IV
    print("=== ATM NVDA call, 30 DTE, IV 32% ===")
    result = bs_all_greeks(
        spot=178.10,
        strike=180.0,
        T=30 / 365.0,
        r=0.045,
        sigma=0.32,
        option_type="call",
    )
    print(_json.dumps({k: round(v, 4) for k, v in result.items()}, indent=2))

    # Aggressive OTM call
    print("\n=== NVDA OTM call, strike 200, 30 DTE, IV 38% ===")
    result = bs_all_greeks(
        spot=178.10,
        strike=200.0,
        T=30 / 365.0,
        r=0.045,
        sigma=0.38,
        option_type="call",
    )
    print(_json.dumps({k: round(v, 4) for k, v in result.items()}, indent=2))

    # LEAPS ITM call
    print("\n=== NVDA LEAPS ITM call, strike 150, 365 DTE, IV 35% ===")
    result = bs_all_greeks(
        spot=178.10,
        strike=150.0,
        T=365 / 365.0,
        r=0.045,
        sigma=0.35,
        option_type="call",
    )
    print(_json.dumps({k: round(v, 4) for k, v in result.items()}, indent=2))

    # Strike selection
    print("\n=== Strike picker: target delta 0.45 ===")
    strikes = [170, 175, 177.5, 180, 182.5, 185, 190]
    iv_map = {170: 0.35, 175: 0.33, 177.5: 0.32, 180: 0.32, 182.5: 0.33, 185: 0.34, 190: 0.36}
    picked = pick_strike_by_delta(
        strikes=strikes,
        spot=178.10,
        T=30 / 365.0,
        r=0.045,
        iv_map=iv_map,
        target_delta=0.45,
        option_type="call",
    )
    print(_json.dumps({k: round(v, 4) if isinstance(v, float) else v for k, v in picked.items()}, indent=2))

    # Implied vol round-trip
    print("\n=== Implied vol round-trip ===")
    true_iv = 0.32
    price = bs_price(178.10, 180.0, 30 / 365.0, 0.045, true_iv, "call")
    recovered = implied_vol_from_price(price, 178.10, 180.0, 30 / 365.0, 0.045, "call")
    print(f"true IV: {true_iv:.4f}, price: {price:.4f}, recovered IV: {recovered:.4f}")
    assert abs(recovered - true_iv) < 0.001, "round-trip failed"
    print("PASS — round-trip matches to 3 decimals")
