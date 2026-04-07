#!/usr/bin/env python3
"""
sectors.py — Ticker → CongressTrades taxonomy sector classifier.

Uses yfinance to fetch the GICS sector + industry for a ticker, then maps
both to the sector tags defined in config/committees.json (matching the
`sector_to_committee` keys).

Cached to data/sector_cache.json so repeated lookups are cheap.

Usage (from Python):
    from sectors import classify_ticker
    sector_tag = classify_ticker("NVDA")  # -> "semiconductors"

CLI:
    python3 scripts/sectors.py NVDA MSFT LMT
"""
import json
import os
import socket
import sys
import time
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from typing import Dict, List, Optional

# Tighter socket timeout so yfinance 404s fail fast instead of hanging.
socket.setdefaulttimeout(4)

try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False


@contextmanager
def _muted_stderr():
    """Silence yfinance's noisy 404 logging during .info calls."""
    devnull = open(os.devnull, "w")
    try:
        with redirect_stderr(devnull):
            yield
    finally:
        devnull.close()

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = BASE_DIR / "data" / "sector_cache.json"

# yfinance industry → CongressTrades sector tag.
# Keys are substrings (case-insensitive) matched against yfinance's `industry`.
INDUSTRY_TO_SECTOR = [
    # Tech / semis / AI
    ("semiconductor", "semiconductors"),
    ("semiconductors", "semiconductors"),
    ("computer hardware", "semiconductors"),
    ("electronic components", "semiconductors"),
    ("software - infrastructure", "cloud_data_centers"),
    ("software - application", "cloud_data_centers"),
    ("information technology services", "cloud_data_centers"),
    ("internet content", "big_tech_antitrust"),
    ("internet retail", "big_tech_antitrust"),
    ("computer hardware", "semiconductors"),
    ("consumer electronics", "china_exposed_tech"),
    # Defense
    ("aerospace & defense", "defense_primes"),
    ("aerospace and defense", "defense_primes"),
    # Healthcare
    ("biotechnology", "biotech"),
    ("drug manufacturers", "pharma"),
    ("medical devices", "medical_devices"),
    ("medical instruments", "medical_devices"),
    ("medical care facilities", "pharma"),
    ("health information", "pharma"),
    ("healthcare plans", "pharma"),
    ("diagnostics", "medical_devices"),
    # Energy
    ("oil & gas e&p", "oil_gas"),
    ("oil & gas integrated", "oil_gas"),
    ("oil & gas midstream", "pipelines"),
    ("oil & gas refining", "oil_gas"),
    ("oil & gas equipment", "oil_gas"),
    ("oil & gas drilling", "oil_gas"),
    ("solar", "renewables"),
    ("utilities - diversified", "utilities"),
    ("utilities - regulated electric", "utilities"),
    ("utilities - regulated gas", "utilities"),
    ("utilities - regulated water", "utilities"),
    ("utilities - renewable", "renewables"),
    ("utilities - independent power producers", "utilities"),
    # Telecom
    ("telecom services", "telecom"),
    # Finance
    ("banks - diversified", "banks"),
    ("banks - regional", "banks"),
    ("banks", "banks"),
    ("capital markets", "financials"),
    ("credit services", "financials"),
    ("insurance", "financials"),
    ("asset management", "financials"),
    ("financial data", "financials"),
    ("financial conglomerates", "financials"),
    # Crypto / fintech
    ("crypto", "crypto"),
    # Cyber
    ("software - infrastructure.*security", "cyber"),
    # Industrials, materials, etc default to broad market
]

# yfinance sector → CongressTrades sector tag (fallback when industry doesn't match)
SECTOR_FALLBACK = {
    "Technology": "broad_market",
    "Financial Services": "financials",
    "Healthcare": "pharma",
    "Energy": "oil_gas",
    "Consumer Cyclical": "broad_market",
    "Consumer Defensive": "broad_market",
    "Industrials": "broad_market",
    "Basic Materials": "broad_market",
    "Utilities": "utilities",
    "Real Estate": "broad_market",
    "Communication Services": "telecom",
}


def _load_cache() -> Dict[str, Optional[str]]:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: Dict[str, Optional[str]]) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


_cache_memo: Optional[Dict[str, Optional[str]]] = None


def _get_cache() -> Dict[str, Optional[str]]:
    global _cache_memo
    if _cache_memo is None:
        _cache_memo = _load_cache()
    return _cache_memo


def classify_ticker(ticker: str, use_cache: bool = True, persist: bool = True) -> Optional[str]:
    """
    Return the CongressTrades sector tag for a ticker, or None if unclassified.
    Cached per-ticker in data/sector_cache.json.
    """
    if not ticker:
        return None
    key = ticker.upper()
    cache = _get_cache()
    if use_cache and key in cache:
        return cache[key]

    if not HAS_YF:
        return None

    try:
        with _muted_stderr():
            info = yf.Ticker(key).info
    except Exception:
        info = {}

    industry = (info.get("industry") or "").lower()
    sector = info.get("sector") or ""

    tag = None
    for needle, taxon in INDUSTRY_TO_SECTOR:
        if needle.lower() in industry:
            tag = taxon
            break
    if not tag and sector in SECTOR_FALLBACK:
        tag = SECTOR_FALLBACK[sector]

    cache[key] = tag
    if persist:
        _save_cache(cache)
    return tag


def classify_batch(tickers: List[str], sleep: float = 0.15) -> Dict[str, Optional[str]]:
    """Bulk classify with rate-limit pacing. Returns {ticker: sector_tag}."""
    out = {}
    for t in tickers:
        out[t] = classify_ticker(t, use_cache=True, persist=False)
        time.sleep(sleep)
    _save_cache(_get_cache())
    return out


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/sectors.py TICKER [TICKER ...]")
        return
    for t in sys.argv[1:]:
        tag = classify_ticker(t)
        print(f"{t}: {tag}")


if __name__ == "__main__":
    main()
