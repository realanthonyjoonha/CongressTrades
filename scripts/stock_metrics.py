#!/usr/bin/env python3
"""
stock_metrics.py — yfinance-derived metrics for the Stage 2 Opportunity
Window Diagnostic (specs/03-diagnostics.md).

Phase 2.3 ships "Stage 2 Lite" — five of the eight checks are computed
here from yfinance data alone. The two MMD-dependent checks (A3 IV
expansion, B1 implied earnings move) are stubbed and will be filled in
by Phase 2.3.5 when MMD is wired.

Computed metrics per ticker:
  - hist_range_45d:    median absolute % move over rolling 45-trading-day windows
  - realized_vol_60d:  60-day annualized realized volatility (% per year)
  - sigma_move(N):     price × (vol/√252) × √N — derived from above
  - sector_vol_60d:    60-day realized vol of the sector ETF
  - sector_etf_move:   % move of sector ETF over an arbitrary window
  - rsi_14:            14-day Wilder RSI
  - actual_price_move: % move of the underlying since trade_date
  - volume_baseline:   20-trading-day average volume up to trade_date
  - volume_spike:      bool — any post-trade day in the window above
                       k_A4_mult × volume_baseline

For v1, all computation is inline per trade. The persistent
data/price_cache.json from backtest.py is reused for close prices, so
repeated lookups are free. Volume data is NOT cached (yfinance returns
it alongside close prices, but the existing cache only stores close).
We accept the per-trade volume fetch for v1; if Phase 2.3.5 needs to
optimize, expand the cache to include OHLCV.

Usage:
    from stock_metrics import compute_metrics_for_trade
    metrics = compute_metrics_for_trade("NVDA", "semiconductors", "2025-08-01")
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False

# We reuse backtest's persistent price cache via direct import.
# Side effect: importing backtest auto-loads the cache.
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest  # noqa: E402

# Trading days per year (used for vol annualization + sigma_move)
TRADING_DAYS_PER_YEAR = 252


# ---------------------------------------------------------------------------
# Sector → ETF map
# ---------------------------------------------------------------------------
# Hard-coded for v1. Maps the CongressTrades sector tags (from sectors.py) to
# liquid sector ETFs whose realized vol is a reasonable proxy for the sector.
# Phase 2.3.5+ may move this to config/committees.json or its own config file.

SECTOR_TO_ETF: Dict[str, str] = {
    "semiconductors": "SOXX",
    "cloud_data_centers": "SKYY",
    "big_tech_antitrust": "QQQ",
    "china_exposed_tech": "QQQ",
    "defense_primes": "ITA",
    "biotech": "IBB",
    "pharma": "XPH",
    "medical_devices": "IHI",
    "oil_gas": "XLE",
    "pipelines": "AMLP",
    "utilities": "XLU",
    "renewables": "ICLN",
    "telecom": "XTL",
    "banks": "KBE",
    "financials": "XLF",
    "crypto": "BITQ",
    "cyber": "HACK",
    "broad_market": "SPY",
    # Fallback for unknown sectors
    None: "SPY",
}


def sector_to_etf(sector_tag: Optional[str]) -> str:
    """Return the sector ETF ticker for a CongressTrades sector tag."""
    if not sector_tag:
        return "SPY"
    return SECTOR_TO_ETF.get(sector_tag, "SPY")


# ---------------------------------------------------------------------------
# Volume helpers (yfinance — NOT cached; small cost per trade)
# ---------------------------------------------------------------------------

_volume_cache: Dict[str, Dict[str, float]] = {}  # ticker → {date_iso: volume}


def _fetch_volume_history(ticker: str) -> Dict[str, float]:
    """
    Fetch 6 years of daily volume data for a ticker. Returns
    {date_iso: volume} or {} if yfinance fails. Cached in-process.
    """
    if not HAS_YF:
        return {}
    if ticker in _volume_cache:
        return _volume_cache[ticker]
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="6y", auto_adjust=True)
        if hist.empty:
            _volume_cache[ticker] = {}
            return {}
        out = {
            idx.strftime("%Y-%m-%d"): float(row["Volume"])
            for idx, row in hist.iterrows()
        }
        _volume_cache[ticker] = out
        return out
    except Exception as e:
        print(f"  [stock_metrics] volume fetch failed for {ticker}: {e}",
              file=sys.stderr)
        _volume_cache[ticker] = {}
        return {}


def compute_volume_baseline(ticker: str, end_date: str, lookback_days: int = 20) -> Optional[float]:
    """
    20-trading-day average volume up to (and including) end_date.
    Returns None if insufficient data.
    """
    vols = _fetch_volume_history(ticker)
    if not vols:
        return None
    sorted_dates = sorted(vols.keys())
    eligible = [d for d in sorted_dates if d <= end_date]
    if len(eligible) < lookback_days:
        return None
    window = eligible[-lookback_days:]
    return sum(vols[d] for d in window) / lookback_days


def compute_volume_spike(
    ticker: str,
    trade_date: str,
    window_days: int = 5,
    spike_mult: float = 2.0,
) -> Optional[Dict]:
    """
    Stage 2 A4: did any day in the [trade_date+1, trade_date+window] range
    have volume > spike_mult × 20-day baseline (computed at trade_date)?
    Returns {fired: bool, max_ratio: float, spike_dates: list} or None if
    we don't have enough data.
    """
    vols = _fetch_volume_history(ticker)
    if not vols:
        return None
    baseline = compute_volume_baseline(ticker, trade_date, lookback_days=20)
    if not baseline or baseline <= 0:
        return None
    sorted_dates = sorted(vols.keys())
    post_trade = [d for d in sorted_dates if d > trade_date][:window_days]
    if not post_trade:
        return None
    spike_dates = []
    max_ratio = 0.0
    for d in post_trade:
        ratio = vols[d] / baseline
        if ratio > max_ratio:
            max_ratio = ratio
        if ratio > spike_mult:
            spike_dates.append(d)
    return {
        "fired": len(spike_dates) > 0,
        "max_ratio": round(max_ratio, 2),
        "spike_dates": spike_dates,
        "baseline_volume": int(baseline),
    }


# ---------------------------------------------------------------------------
# Range, vol, RSI helpers (use backtest's cached price data)
# ---------------------------------------------------------------------------

def _get_price_series(ticker: str) -> Dict[str, float]:
    """
    Get the cached close-price dict for a ticker, NaN-filtered.

    Triggers a yfinance fetch on first miss (via backtest.get_close
    indirection). Returns empty dict if data unavailable.

    yfinance occasionally returns NaN for "today" when markets are still
    open or the bar is partial. We filter those out so downstream stats
    code doesn't have to defensively handle math errors.
    """
    today = datetime.utcnow().strftime("%Y-%m-%d")
    backtest.get_close(ticker, today)
    raw = backtest._price_cache.get(ticker, {})
    if not raw:
        return {}
    # Filter NaN / non-positive entries
    return {
        d: v for d, v in raw.items()
        if isinstance(v, (int, float))
        and not (isinstance(v, float) and math.isnan(v))
        and v > 0
    }


def _latest_available_date(ticker: str) -> Optional[str]:
    """
    Latest non-NaN price date in the cache for a ticker. Used as the
    "as of" date when scoring trades — better than literal today because
    yfinance may not have today's close yet.
    """
    series = _get_price_series(ticker)
    if not series:
        return None
    return sorted(series.keys())[-1]


def compute_hist_range_45d(ticker: str) -> Optional[float]:
    """
    Median absolute % price move over rolling 45-trading-day windows
    over the last 1 year. Returns the value as a decimal fraction
    (e.g. 0.06 = 6%).
    """
    prices = _get_price_series(ticker)
    if not prices or len(prices) < 60:
        return None
    sorted_dates = sorted(prices.keys())
    # Use last ~252 trading days
    recent = sorted_dates[-252:] if len(sorted_dates) >= 252 else sorted_dates
    moves = []
    for i in range(45, len(recent)):
        p_start = prices[recent[i - 45]]
        p_end = prices[recent[i]]
        if p_start > 0:
            moves.append(abs(p_end / p_start - 1))
    if not moves:
        return None
    moves.sort()
    return moves[len(moves) // 2]


def compute_realized_vol_60d(ticker: str) -> Optional[float]:
    """
    60-day annualized realized volatility (decimal fraction, e.g. 0.30 = 30%/yr).
    Standard daily-return stdev × √252.
    """
    prices = _get_price_series(ticker)
    if not prices or len(prices) < 65:
        return None
    sorted_dates = sorted(prices.keys())
    recent = sorted_dates[-61:]  # need 61 to compute 60 returns
    rets = []
    for i in range(1, len(recent)):
        p_prev = prices[recent[i - 1]]
        p_now = prices[recent[i]]
        if p_prev > 0:
            rets.append(math.log(p_now / p_prev))
    if len(rets) < 30:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    daily_std = math.sqrt(var)
    return daily_std * math.sqrt(TRADING_DAYS_PER_YEAR)


def sigma_move(price: float, realized_vol: float, days: int) -> float:
    """
    Expected price move at 1 sigma over N calendar days.
    Standard formula: price × vol × √(N/252).
    Returns absolute price units (not %).
    """
    return price * realized_vol * math.sqrt(days / TRADING_DAYS_PER_YEAR)


def compute_actual_move(ticker: str, trade_date: str, end_date: Optional[str] = None) -> Optional[Dict]:
    """
    Actual price move from trade_date to end_date.

    end_date default: latest non-NaN close in the cache (typically yesterday
    or today, depending on whether yfinance has the closing bar yet).
    Falls back to literal today only if the cache is empty (which would
    also fail the entry lookup).

    Returns {entry_price, exit_price, abs_pct_move, signed_pct_move,
    days_elapsed} or None if price data missing.
    """
    if end_date is None:
        end_date = _latest_available_date(ticker) or datetime.utcnow().strftime("%Y-%m-%d")

    series = _get_price_series(ticker)
    if not series:
        return None

    # Find entry: closest available date >= trade_date
    sorted_dates = sorted(series.keys())
    entry_date = next((d for d in sorted_dates if d >= trade_date), None)
    if not entry_date:
        return None
    p_entry = series[entry_date]

    # Find exit: closest available date <= end_date
    exit_date = None
    for d in reversed(sorted_dates):
        if d <= end_date:
            exit_date = d
            break
    if not exit_date:
        return None
    p_exit = series[exit_date]

    if p_entry <= 0 or p_exit <= 0:
        return None

    signed = (p_exit / p_entry) - 1
    try:
        td = datetime.strptime(trade_date, "%Y-%m-%d").date()
        ed = datetime.strptime(end_date, "%Y-%m-%d").date()
        days = (ed - td).days
    except ValueError:
        days = 0

    return {
        "entry_price": p_entry,
        "exit_price": p_exit,
        "entry_date": entry_date,
        "exit_date": exit_date,
        "signed_pct_move": signed,
        "abs_pct_move": abs(signed),
        "days_elapsed": days,
    }


def compute_rsi(ticker: str, end_date: str, period: int = 14) -> Optional[float]:
    """
    14-day Wilder RSI on close prices, evaluated at end_date.
    Returns float in [0, 100] or None if insufficient data.
    """
    prices = _get_price_series(ticker)
    if not prices:
        return None
    sorted_dates = sorted(prices.keys())
    eligible = [d for d in sorted_dates if d <= end_date]
    if len(eligible) < period + 1:
        return None
    window = eligible[-(period + 1):]
    gains = []
    losses = []
    for i in range(1, len(window)):
        diff = prices[window[i]] - prices[window[i - 1]]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-diff)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def compute_sector_metrics(sector_tag: Optional[str], trade_date: str) -> Dict:
    """
    Compute sector ETF realized vol + the sector's % move since trade_date.
    Returns {sector_etf, sector_vol_60d, sector_etf_move, days_elapsed}.
    """
    etf = sector_to_etf(sector_tag)
    vol = compute_realized_vol_60d(etf)
    move_data = compute_actual_move(etf, trade_date)
    return {
        "sector_etf": etf,
        "sector_vol_60d": vol,
        "sector_etf_move": move_data["signed_pct_move"] if move_data else None,
        "sector_days_elapsed": move_data["days_elapsed"] if move_data else None,
    }


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def get_next_earnings(ticker: str) -> Optional[Dict]:
    """
    Fetch the next earnings date + analyst estimates via yfinance.
    Returns dict {next_earnings_date, eps_avg, eps_high, eps_low,
    revenue_avg} or None if unavailable.

    Uses yfinance Ticker.calendar (NOT earnings_dates which requires lxml).
    The calendar shows ONE upcoming earnings date — for past earnings
    history we'd need a different source. For Phase 2.3.5 the next-only
    view is enough: it feeds into Stage 3 forward catalyst identification
    and into the options layer's DTE selection.
    """
    if not HAS_YF:
        return None
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar
        if not cal:
            return None
        earnings_dates = cal.get("Earnings Date") or []
        if not earnings_dates:
            return None
        next_date = earnings_dates[0]
        # yfinance returns datetime.date objects; convert to ISO string
        if hasattr(next_date, "isoformat"):
            next_date = next_date.isoformat()
        return {
            "next_earnings_date": next_date,
            "eps_avg": cal.get("Earnings Average"),
            "eps_high": cal.get("Earnings High"),
            "eps_low": cal.get("Earnings Low"),
            "revenue_avg": cal.get("Revenue Average"),
        }
    except Exception as e:
        print(f"  [stock_metrics] earnings calendar fetch failed for {ticker}: {e}",
              file=sys.stderr)
        return None


def compute_metrics_for_trade(
    ticker: str,
    sector: Optional[str],
    trade_date: str,
    transaction_type: str = "buy",
    k_A4_window: int = 5,
    k_A4_mult: float = 2.0,
) -> Dict:
    """
    Compute every Stage 2 Lite input for one trade. Returns a single
    dict that pipeline.compute_owd_score() consumes.

    All values may be None if yfinance lookups fail; pipeline.py is
    responsible for handling missing data gracefully.
    """
    out: Dict = {
        "ticker": ticker,
        "sector": sector,
        "trade_date": trade_date,
        "transaction_type": transaction_type,
    }

    # Range + vol
    out["hist_range_45d"] = compute_hist_range_45d(ticker)
    out["realized_vol_60d"] = compute_realized_vol_60d(ticker)

    # Actual move since trade
    move_data = compute_actual_move(ticker, trade_date)
    if move_data:
        out["entry_price"] = move_data["entry_price"]
        out["current_price"] = move_data["exit_price"]
        out["actual_move_pct"] = move_data["signed_pct_move"]
        out["actual_move_abs_pct"] = move_data["abs_pct_move"]
        out["days_elapsed"] = move_data["days_elapsed"]
    else:
        out["entry_price"] = None
        out["current_price"] = None
        out["actual_move_pct"] = None
        out["actual_move_abs_pct"] = None
        out["days_elapsed"] = 0

    # Sigma move at days_elapsed
    if (
        out["entry_price"]
        and out["realized_vol_60d"] is not None
        and out["days_elapsed"]
    ):
        out["sigma_move_pct"] = (
            out["realized_vol_60d"] * math.sqrt(out["days_elapsed"] / TRADING_DAYS_PER_YEAR)
        )
    else:
        out["sigma_move_pct"] = None

    # RSI on today (or end of cached data)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    out["rsi_14"] = compute_rsi(ticker, today)

    # Volume spike check
    out["volume_spike"] = compute_volume_spike(
        ticker, trade_date, window_days=k_A4_window, spike_mult=k_A4_mult
    )

    # Sector ETF metrics
    sector_data = compute_sector_metrics(sector, trade_date)
    out.update(sector_data)

    # MMD-deferred fields (Phase 2.3.5 will populate these)
    out["iv_percentile_current"] = None  # requires historical IV cache (Phase 3)
    out["iv_range_12m"] = None  # requires historical IV cache (Phase 3)
    out["implied_earnings_move"] = None  # requires options snapshot (paid MMD tier)

    # Earnings calendar (Phase 2.3.5: yfinance, free)
    earnings = get_next_earnings(ticker)
    if earnings:
        out["next_earnings_date"] = earnings.get("next_earnings_date")
        out["next_earnings_eps_avg"] = earnings.get("eps_avg")
        out["next_earnings_eps_high"] = earnings.get("eps_high")
        out["next_earnings_eps_low"] = earnings.get("eps_low")
        out["next_earnings_revenue_avg"] = earnings.get("revenue_avg")
    else:
        out["next_earnings_date"] = None

    return out


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json
    if len(sys.argv) < 4:
        print("Usage: python3 stock_metrics.py TICKER SECTOR TRADE_DATE")
        print('Example: python3 stock_metrics.py NVDA semiconductors 2025-08-01')
        sys.exit(1)
    ticker, sector, trade_date = sys.argv[1:4]
    m = compute_metrics_for_trade(ticker, sector, trade_date)
    # Convert any remaining float precision for readable output
    def _r(v):
        if isinstance(v, float):
            return round(v, 6)
        return v
    print(_json.dumps({k: _r(v) for k, v in m.items()}, indent=2, default=str))
    backtest.save_price_cache()
