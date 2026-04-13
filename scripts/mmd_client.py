#!/usr/bin/env python3
"""
mmd_client.py — Thin Python wrapper for the Massive Market Data REST API.

Phase 2.3.5: this module exists to give CongressTrades a clean integration
point for MMD data. The CURRENT subscription tier supports:
  - Reference data (tickers, contract listings)
  - Aggregate bars / OHLC for stocks, options, indices, futures

NOT supported on this tier (would need an upgrade):
  - Options chain snapshot (live bid/ask/IV per strike in one call)
  - Historical options quotes (per-trade bid/ask)
  - Earnings calendar (Benzinga, TMX corporate events)
  - Financial statements

Because the snapshot endpoint isn't available, the live options layer
in scripts/options_chain.py uses yfinance for chains and computes IV
via inverse Black-Scholes from lastPrice. This module provides MMD
backups for the endpoints that DO work, plus a clear upgrade path: when
the user upgrades the MMD tier, the snapshot/earnings methods here can
be wired into options_chain.py with a one-line backend swap.

Usage:
    from mmd_client import MMDClient
    client = MMDClient()  # uses MMD_API_KEY env var
    bars = client.get_stock_aggregates("NVDA", "2025-11-01", "2025-12-01")
    contracts = client.list_option_contracts("NVDA", expiration_gte="2026-04-01")
    prev = client.get_option_prev_close("O:NVDA260618C00186000")

The wrapper deliberately:
  - Uses urllib (no `requests` dep)
  - Returns parsed dicts/lists, not raw CSV
  - Handles 403s gracefully (returns None and logs the gap)
  - Has a 30-second hard timeout per request
  - Does NOT cache (caller is responsible — most calls feed into
    backtest._price_cache or stock_metrics.json which already cache)
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# Default base URL — Massive Market Data REST API
MMD_BASE_URL = os.environ.get("MMD_BASE_URL", "https://api.massive.com")
MMD_API_KEY_ENV = "MMD_API_KEY"

DEFAULT_TIMEOUT_SEC = 30
DEFAULT_USER_AGENT = "CongressTrades/1.0 (research; contact: research@apesdegen.com)"


class MMDError(Exception):
    """Base for MMD client errors."""


class MMDAuthError(MMDError):
    """403 — endpoint not entitled on this subscription tier."""


class MMDClient:
    """Lightweight HTTP client for the Massive Market Data REST API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = MMD_BASE_URL,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        user_agent: str = DEFAULT_USER_AGENT,
    ):
        # API key resolution: explicit > env var > config file > None
        self.api_key = api_key or os.environ.get(MMD_API_KEY_ENV)
        if not self.api_key:
            self.api_key = self._load_key_from_config()
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.user_agent = user_agent

    def _load_key_from_config(self) -> Optional[str]:
        """Try config/mmd.json as a fallback location for the API key."""
        cfg_path = (
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "config", "mmd.json")
        )
        if not os.path.exists(cfg_path):
            return None
        try:
            with open(cfg_path) as f:
                cfg = json.load(f)
            return cfg.get("api_key")
        except (json.JSONDecodeError, OSError):
            return None

    # ---- Low-level HTTP ----

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        """
        GET an MMD endpoint. Returns raw response body as a string.
        Adds the API key as a query param. Raises MMDAuthError on 403,
        MMDError on other failures.
        """
        if not self.api_key:
            raise MMDError("No MMD API key configured. Set MMD_API_KEY env "
                           "var or create config/mmd.json with {\"api_key\": ...}")

        params = dict(params or {})
        params["apiKey"] = self.api_key

        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "text/csv,application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8") if e.fp else ""
            except Exception:
                pass
            if e.code == 403:
                raise MMDAuthError(
                    f"403 not entitled on path {path}: {body}"
                )
            raise MMDError(f"MMD HTTP {e.code} on {path}: {body}")
        except urllib.error.URLError as e:
            raise MMDError(f"MMD URL error on {path}: {e.reason}")

    def _parse_response(self, body: str) -> List[Dict]:
        """
        Parse an MMD response body. Handles both JSON and CSV formats:
        - Direct API calls (with apiKey param) return JSON with a
          `results` array
        - The MCP wrapper returns CSV

        Normalizes all dict keys to lowercase for consistent access.
        """
        if not body or not body.strip():
            return []

        body_stripped = body.strip()

        # ---- JSON path (direct API) ----
        # Do NOT lowercase JSON keys — the aggregates endpoints have both
        # "T" (ticker symbol) and "t" (timestamp ms), and lowercasing
        # causes a key collision. JSON keys are used as-is; the accessor
        # code in each method handles both casing patterns.
        if body_stripped.startswith("{") or body_stripped.startswith("["):
            try:
                parsed = json.loads(body_stripped)
                if isinstance(parsed, dict):
                    results = parsed.get("results", [])
                    if isinstance(results, list):
                        return [item for item in results if isinstance(item, dict)]
                    return [parsed]
                if isinstance(parsed, list):
                    return [item for item in parsed if isinstance(item, dict)]
            except json.JSONDecodeError:
                pass  # Fall through to CSV

        # ---- CSV path (MCP wrapper) ----
        lines = body.split("\n")
        csv_lines: List[str] = []
        for line in lines:
            if not line.strip() and csv_lines:
                break
            csv_lines.append(line)
        if csv_lines:
            csv_lines[0] = csv_lines[0].lower()
        reader = csv.DictReader(io.StringIO("\n".join(csv_lines)))
        return list(reader)

    # ---- Stock aggregates (works on current tier) ----

    def get_stock_aggregates(
        self,
        ticker: str,
        from_date: str,
        to_date: str,
        timespan: str = "day",
        multiplier: int = 1,
        limit: int = 5000,
    ) -> List[Dict]:
        """
        Stock OHLC aggregate bars for a date range.
        Returns list of dicts with keys: o (open), h (high), l (low),
        c (close), v (volume), vw (vwap), t (timestamp ms), n (num trades).

        Equivalent to yfinance Ticker.history but from MMD's paid feed.
        """
        path = (
            f"/v2/aggs/ticker/{ticker.upper()}/range/{multiplier}/{timespan}"
            f"/{from_date}/{to_date}"
        )
        try:
            body = self._get(path, params={"limit": limit, "sort": "asc"})
        except MMDError as e:
            print(f"  [mmd] get_stock_aggregates({ticker}) failed: {e}", file=sys.stderr)
            return []

        rows = self._parse_response(body)
        out: List[Dict] = []
        for r in rows:
            try:
                out.append({
                    "open": float(r["o"]),
                    "high": float(r["h"]),
                    "low": float(r["l"]),
                    "close": float(r["c"]),
                    "volume": float(r["v"]),
                    "vwap": float(r.get("vw", 0) or 0),
                    "timestamp_ms": int(r["t"]),
                    "num_trades": int(r.get("n", 0) or 0),
                })
            except (KeyError, ValueError):
                continue
        return out

    # ---- Options reference + aggregates (works on current tier) ----

    def list_option_contracts(
        self,
        underlying: str,
        contract_type: str = "call",
        expiration_gte: Optional[str] = None,
        expiration_lte: Optional[str] = None,
        strike_gte: Optional[float] = None,
        strike_lte: Optional[float] = None,
        limit: int = 250,
    ) -> List[Dict]:
        """
        List option contracts for an underlying ticker, optionally filtered
        by contract type, expiration window, and strike range.
        Returns list of dicts with keys: ticker (the option symbol),
        strike_price, expiration_date, contract_type, exercise_style.
        """
        params: Dict[str, Any] = {
            "underlying_ticker": underlying.upper(),
            "contract_type": contract_type,
            "limit": min(limit, 250),
            "order": "asc",
            "sort": "expiration_date",
        }
        if expiration_gte:
            params["expiration_date.gte"] = expiration_gte
        if expiration_lte:
            params["expiration_date.lte"] = expiration_lte
        if strike_gte is not None:
            params["strike_price.gte"] = strike_gte
        if strike_lte is not None:
            params["strike_price.lte"] = strike_lte

        try:
            body = self._get("/v3/reference/options/contracts", params=params)
        except MMDError as e:
            print(f"  [mmd] list_option_contracts({underlying}) failed: {e}", file=sys.stderr)
            return []

        rows = self._parse_response(body)
        out: List[Dict] = []
        for r in rows:
            try:
                # Handle: JSON PascalCase, JSON snake_case, CSV lowered
                def _g(r, *keys, default=""):
                    for k in keys:
                        v = r.get(k)
                        if v is not None:
                            return v
                    return default

                ticker = _g(r, "ticker", "Ticker")
                strike = _g(r, "strike_price", "StrikePrice", "strikeprice", default="0")
                expiry = _g(r, "expiration_date", "ExpirationDate", "expirationdate")
                ctype = _g(r, "contract_type", "ContractType", "contracttype")
                estyle = _g(r, "exercise_style", "ExerciseStyle", "exercisestyle")
                uticker = _g(r, "underlying_ticker", "UnderlyingTicker", "underlyingticker",
                             default=underlying.upper())

                out.append({
                    "ticker": str(ticker),
                    "strike_price": float(strike),
                    "expiration_date": str(expiry),
                    "contract_type": str(ctype),
                    "exercise_style": str(estyle),
                    "underlying_ticker": str(uticker),
                })
            except (KeyError, ValueError):
                continue
        return out

    def get_option_aggregates(
        self,
        option_ticker: str,
        from_date: str,
        to_date: str,
        timespan: str = "day",
        multiplier: int = 1,
        limit: int = 5000,
    ) -> List[Dict]:
        """
        Historical OHLC bars for a specific option contract.
        option_ticker is the MMD format like "O:NVDA260618C00186000".

        Useful for backfilling historical IV (compute via inverse BS from
        each day's close) or for the Phase 3 feedback loop's
        retroactive scoring of recommended trades.
        """
        path = (
            f"/v2/aggs/ticker/{option_ticker}/range/{multiplier}/{timespan}"
            f"/{from_date}/{to_date}"
        )
        try:
            body = self._get(path, params={"limit": limit, "sort": "asc"})
        except MMDError as e:
            print(f"  [mmd] get_option_aggregates({option_ticker}) failed: {e}",
                  file=sys.stderr)
            return []
        rows = self._parse_response(body)
        out: List[Dict] = []
        for r in rows:
            try:
                out.append({
                    "open": float(r["o"]),
                    "high": float(r["h"]),
                    "low": float(r["l"]),
                    "close": float(r["c"]),
                    "volume": float(r["v"]),
                    "vwap": float(r.get("vw", 0) or 0),
                    "timestamp_ms": int(r["t"]),
                    "num_trades": int(r.get("n", 0) or 0),
                })
            except (KeyError, ValueError):
                continue
        return out

    def get_option_prev_close(self, option_ticker: str) -> Optional[Dict]:
        """
        Previous trading day's OHLC for a specific option contract.
        Returns a single dict or None on failure.
        """
        path = f"/v2/aggs/ticker/{option_ticker}/prev"
        try:
            body = self._get(path)
        except MMDError as e:
            print(f"  [mmd] get_option_prev_close({option_ticker}) failed: {e}",
                  file=sys.stderr)
            return None
        rows = self._parse_response(body)
        if not rows:
            return None
        r = rows[0]
        try:
            return {
                "ticker": r.get("T", option_ticker),
                "open": float(r["o"]),
                "high": float(r["h"]),
                "low": float(r["l"]),
                "close": float(r["c"]),
                "volume": float(r["v"]),
                "vwap": float(r.get("vw", 0) or 0),
                "timestamp_ms": int(r["t"]),
                "num_trades": int(r.get("n", 0) or 0),
            }
        except (KeyError, ValueError):
            return None

    # ---- Reference data ----

    def get_ticker_reference(self, ticker: str) -> Optional[Dict]:
        """
        Stock metadata (name, market cap, sector, exchange, etc).
        Returns a dict or None on failure.
        """
        try:
            body = self._get(f"/v3/reference/tickers/{ticker.upper()}")
        except MMDError as e:
            print(f"  [mmd] get_ticker_reference({ticker}) failed: {e}", file=sys.stderr)
            return None
        rows = self._parse_response(body)
        if not rows:
            return None
        return rows[0]

    # ---- Capability check ----

    def capability_check(self) -> Dict[str, bool]:
        """
        Probe each endpoint family with a small request to discover what
        the current API key has access to. Useful for diagnostics.
        Returns {endpoint_name: is_accessible} dict.
        """
        results: Dict[str, bool] = {}

        # Stock aggregates
        try:
            self._get("/v2/aggs/ticker/SPY/range/1/day/2025-12-01/2025-12-02",
                      params={"limit": 1})
            results["stock_aggregates"] = True
        except MMDAuthError:
            results["stock_aggregates"] = False
        except MMDError:
            results["stock_aggregates"] = None  # network error, not entitlement

        # Option contracts list
        try:
            self._get("/v3/reference/options/contracts",
                      params={"underlying_ticker": "SPY", "limit": 1})
            results["option_contracts"] = True
        except MMDAuthError:
            results["option_contracts"] = False
        except MMDError:
            results["option_contracts"] = None

        # Option chain snapshot (paid)
        try:
            self._get("/v3/snapshot/options/SPY", params={"limit": 1})
            results["option_snapshot"] = True
        except MMDAuthError:
            results["option_snapshot"] = False
        except MMDError:
            results["option_snapshot"] = None

        # Earnings (paid)
        try:
            self._get("/benzinga/v1/earnings",
                      params={"ticker": "SPY", "limit": 1})
            results["earnings_benzinga"] = True
        except MMDAuthError:
            results["earnings_benzinga"] = False
        except MMDError:
            results["earnings_benzinga"] = None

        return results


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json

    if len(sys.argv) >= 2 and sys.argv[1] == "capability":
        print("Probing MMD endpoint capability for current API key...")
        client = MMDClient()
        caps = client.capability_check()
        print(_json.dumps(caps, indent=2))
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == "stock":
        ticker = sys.argv[2] if len(sys.argv) > 2 else "NVDA"
        client = MMDClient()
        bars = client.get_stock_aggregates(ticker, "2025-12-01", "2025-12-05")
        print(f"{ticker} bars: {len(bars)}")
        for b in bars:
            print(_json.dumps(b))
        sys.exit(0)

    if len(sys.argv) >= 2 and sys.argv[1] == "contracts":
        ticker = sys.argv[2] if len(sys.argv) > 2 else "NVDA"
        client = MMDClient()
        contracts = client.list_option_contracts(
            ticker,
            expiration_gte="2026-05-01",
            expiration_lte="2026-06-30",
            strike_gte=170,
            strike_lte=200,
            limit=10,
        )
        print(f"{ticker} contracts: {len(contracts)}")
        for c in contracts[:5]:
            print(_json.dumps(c))
        sys.exit(0)

    print("Usage: python3 mmd_client.py [capability|stock|contracts] [TICKER]")
