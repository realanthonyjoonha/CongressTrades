#!/usr/bin/env python3
"""
charts.py — matplotlib chart generation for email embedding.

Produces base64-encoded PNG data URIs that can be embedded directly in
MJML templates via <mj-image src="data:image/png;base64,..."/>. No
external hosting required; charts render inline in Gmail, Apple Mail,
Outlook 365, ProtonMail, and mobile clients.

Chart types:
  - sparkline_cumulative_excess: tiny 200×60 line showing cumulative
    excess vs SPY since trade_date. Used in Smart Money open-position
    detail blocks.
  - price_chart_since_trade: 500×200 price chart with SPY benchmark
    overlay and entry marker. Used for per-trade cards in daily digest.
  - tier_donut: 300×300 donut of STRONG/BASE/MODERATE/SKIP counts.
    Used at top of weekly retrospective.
  - winners_losers_bar: 500×300 horizontal bar of top winners / losers
    by excess vs SPY. Used in weekly retrospective.

All functions return Optional[str]: the base64 data URI on success,
None on failure (missing data, matplotlib error). Callers should
gracefully skip charts when None is returned.

Theme matches the MJML email branding: dark GitHub-style background
(#0d1117), accent blue (#58a6ff), muted gray (#8b949e), success green
(#3fb950), warning orange (#d29922), error red (#f85149).
"""
from __future__ import annotations

import base64
import io
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

try:
    import matplotlib
    matplotlib.use("Agg")  # Headless backend — no display needed
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrowPatch  # noqa: F401
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest  # noqa: E402

# ---------------------------------------------------------------------------
# Brand colors (match templates/_shell.mjml.j2)
# ---------------------------------------------------------------------------

COLOR_BG = "#0d1117"
COLOR_PANEL = "#161b22"
COLOR_BORDER = "#21262d"
COLOR_TEXT = "#e6edf3"
COLOR_MUTED = "#8b949e"
COLOR_ACCENT = "#58a6ff"
COLOR_SUCCESS = "#3fb950"
COLOR_WARNING = "#d29922"
COLOR_ERROR = "#f85149"
COLOR_PURPLE = "#a371f7"

# Tier colors (match MJML badges)
TIER_COLORS = {
    "STRONG": "#238636",
    "BASE": "#1f6feb",
    "MODERATE": "#656d76",
    "SKIP": "#30363d",
}


# ---------------------------------------------------------------------------
# Chart registry — placeholder token + sidecar pattern
# ---------------------------------------------------------------------------
#
# Charts are embedded in agent emails via the following pattern:
#
#   1. Phase A Python driver generates the chart, stores the data URI in a
#      "registry" (an in-memory dict)
#   2. The markdown research pack contains only a short placeholder token
#      like `<!--CHART:abc123-->` — NOT the 35KB base64 payload
#   3. When Phase A is done, the registry is serialized to a sidecar JSON
#      file next to the pack (e.g. `daily_TIMESTAMP_pack.md` +
#      `daily_TIMESTAMP_charts.json`)
#   4. The LLM reads the tiny pack, writes narrative. It's instructed to
#      preserve HTML comments like `<!--CHART:abc123-->` verbatim
#      (this is natural — HTML comments don't affect rendering)
#   5. format_report.py loads the sidecar, walks the narrative, replaces
#      each `<!--CHART:ID-->` with `![alt](data:image/png;base64,...)`
#      BEFORE handing the markdown to markdown2
#
# This keeps the LLM prompt small (5-20KB vs 180KB with embedded base64),
# avoids ~$180/yr in redundant input-token costs at typical frequency,
# and makes chart presence robust to LLM truncation.

import hashlib as _hashlib
from typing import Tuple

CHART_PLACEHOLDER_PREFIX = "<!--CHART:"
CHART_PLACEHOLDER_SUFFIX = "-->"


class ChartRegistry:
    """
    In-memory registry of chart_id → (data_uri, alt_text).
    Each agent's Phase A driver instantiates one of these, generates
    charts via the helpers below, and serializes to a sidecar JSON at
    the end of the run.
    """

    def __init__(self) -> None:
        self._charts: Dict[str, Dict[str, str]] = {}

    def add(self, key: str, data_uri: str, alt_text: str = "") -> str:
        """
        Register a chart under a deterministic key. Returns the
        markdown-safe placeholder token that should be embedded in the
        pack.
        """
        chart_id = self._hash_id(key)
        self._charts[chart_id] = {"uri": data_uri, "alt": alt_text or key}
        return self.placeholder(chart_id)

    @staticmethod
    def placeholder(chart_id: str) -> str:
        return f"{CHART_PLACEHOLDER_PREFIX}{chart_id}{CHART_PLACEHOLDER_SUFFIX}"

    @staticmethod
    def _hash_id(key: str) -> str:
        """Stable 12-character hex id from a longer key."""
        return _hashlib.blake2b(key.encode("utf-8"), digest_size=6).hexdigest()

    def to_dict(self) -> Dict[str, Dict[str, str]]:
        return dict(self._charts)

    def save(self, path) -> None:
        """Write the registry to a JSON sidecar file."""
        import json as _json
        from pathlib import Path as _Path
        _Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            _json.dump(self._charts, f, indent=2)

    @classmethod
    def load(cls, path) -> "ChartRegistry":
        """Load a registry from a JSON sidecar file."""
        import json as _json
        from pathlib import Path as _Path
        obj = cls()
        p = _Path(path)
        if not p.exists():
            return obj
        try:
            with open(p) as f:
                obj._charts = _json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
        return obj

    def substitute(self, text: str) -> str:
        """
        Replace all <!--CHART:id--> placeholders in `text` with inline
        markdown image tags using the registered data URI. Missing ids
        are left as-is (silent passthrough — format_report strips them).
        """
        import re as _re
        pattern = _re.compile(
            _re.escape(CHART_PLACEHOLDER_PREFIX)
            + r"([a-f0-9]+)"
            + _re.escape(CHART_PLACEHOLDER_SUFFIX)
        )

        def _sub(m):
            cid = m.group(1)
            entry = self._charts.get(cid)
            if not entry:
                return ""  # drop missing placeholders
            alt = entry.get("alt", "chart")
            uri = entry.get("uri", "")
            if not uri:
                return ""
            return f"![{alt}]({uri})"

        return pattern.sub(_sub, text)


# ---------------------------------------------------------------------------
# Registry-aware helpers (return placeholders, register the actual URI)
# ---------------------------------------------------------------------------

def register_sparkline(
    registry: ChartRegistry,
    ticker: str,
    trade_date: str,
) -> Optional[str]:
    """Generate sparkline and register it. Returns placeholder or None."""
    uri = sparkline_cumulative_excess(ticker, trade_date)
    if not uri:
        return None
    key = f"sparkline:{ticker}:{trade_date}"
    alt = f"{ticker} excess vs SPY since {trade_date}"
    return registry.add(key, uri, alt)


def register_price_chart(
    registry: ChartRegistry,
    ticker: str,
    trade_date: str,
    trade_id: Optional[int] = None,
) -> Optional[str]:
    """Generate per-trade price chart and register. Returns placeholder or None."""
    uri = price_chart_since_trade(ticker, trade_date)
    if not uri:
        return None
    key = f"trade_chart:{trade_id or ''}:{ticker}:{trade_date}"
    alt = f"{ticker} vs SPY since {trade_date}"
    return registry.add(key, uri, alt)


def register_tier_donut(
    registry: ChartRegistry,
    counts: Dict[str, int],
    scope_label: str = "weekly",
) -> Optional[str]:
    """Generate tier donut and register. Returns placeholder or None."""
    uri = tier_donut(counts)
    if not uri:
        return None
    key = f"tier_donut:{scope_label}:{sorted(counts.items())}"
    alt = "Tier breakdown"
    return registry.add(key, uri, alt)


def register_winners_losers_bar(
    registry: ChartRegistry,
    winners: List[Dict],
    losers: List[Dict],
    scope_label: str = "weekly",
) -> Optional[str]:
    """Generate winners/losers bar chart and register. Returns placeholder or None."""
    uri = winners_losers_bar(winners, losers)
    if not uri:
        return None
    key = f"winners_losers:{scope_label}:{len(winners)}:{len(losers)}"
    alt = "Weekly winners and losers vs SPY"
    return registry.add(key, uri, alt)


# Need json module at module level for ChartRegistry.save/load above
import json  # noqa: E402  (placed after helpers to avoid top-of-file churn)


# ---------------------------------------------------------------------------
# Base helpers
# ---------------------------------------------------------------------------

def _fig_to_data_uri(fig, dpi: int = 110) -> Optional[str]:
    """Serialize a matplotlib figure to a base64 data URI PNG."""
    try:
        buf = io.BytesIO()
        fig.savefig(
            buf,
            format="png",
            dpi=dpi,
            facecolor=fig.get_facecolor(),
            edgecolor="none",
            bbox_inches="tight",
            pad_inches=0.05,
        )
        buf.seek(0)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        print(f"  [charts] figure serialization failed: {e}", file=sys.stderr)
        return None
    finally:
        plt.close(fig)


def _get_price_series_since(ticker: str, start_date: str) -> Optional[List[Dict]]:
    """
    Return a sorted list of {date, close} for `ticker` from start_date
    to the latest cached date. Uses backtest's persistent price cache,
    which is NaN-filtered and populated via yfinance.

    Returns None if data unavailable.
    """
    # Warm the cache (triggers yfinance fetch if missing)
    backtest.get_close(ticker, start_date)
    raw = backtest._price_cache.get(ticker, {})
    if not raw:
        return None

    # Filter to valid entries since start_date
    series = []
    for date_iso, close in raw.items():
        if date_iso < start_date:
            continue
        try:
            fclose = float(close)
            import math
            if math.isnan(fclose) or fclose <= 0:
                continue
        except (TypeError, ValueError):
            continue
        series.append({"date": date_iso, "close": fclose})
    if not series:
        return None
    series.sort(key=lambda r: r["date"])
    return series


def _cumulative_pct_move(series: List[Dict]) -> List[float]:
    """Cumulative % move from the first point. Output is signed decimal."""
    if not series:
        return []
    base = series[0]["close"]
    return [(p["close"] / base - 1.0) for p in series]


# ---------------------------------------------------------------------------
# Chart 1: sparkline (Smart Money open positions)
# ---------------------------------------------------------------------------

def sparkline_cumulative_excess(
    ticker: str,
    trade_date: str,
    width_px: int = 200,
    height_px: int = 50,
) -> Optional[str]:
    """
    Tiny sparkline showing cumulative excess vs SPY since trade_date.
    Ideal for inline email display next to an open position summary.

    Line color: green if cumulative excess is positive, red if negative.
    No axes, no labels — just the shape of the curve.

    Returns base64 data URI or None.
    """
    if not HAS_MATPLOTLIB:
        return None

    stock_series = _get_price_series_since(ticker, trade_date)
    spy_series = _get_price_series_since("SPY", trade_date)
    if not stock_series or not spy_series:
        return None

    # Align by date (intersection) so the excess math is honest
    spy_by_date = {p["date"]: p["close"] for p in spy_series}
    aligned_stock: List[Dict] = []
    aligned_spy: List[Dict] = []
    for p in stock_series:
        if p["date"] in spy_by_date:
            aligned_stock.append(p)
            aligned_spy.append({"date": p["date"], "close": spy_by_date[p["date"]]})
    if len(aligned_stock) < 3:
        return None

    stock_cum = _cumulative_pct_move(aligned_stock)
    spy_cum = _cumulative_pct_move(aligned_spy)
    excess = [s - p for s, p in zip(stock_cum, spy_cum)]

    end_excess = excess[-1]
    color = COLOR_SUCCESS if end_excess >= 0 else COLOR_ERROR
    fill_alpha = 0.25

    fig, ax = plt.subplots(
        figsize=(width_px / 100, height_px / 100),
        dpi=100,
        facecolor=COLOR_PANEL,
    )
    ax.set_facecolor(COLOR_PANEL)
    ax.plot(range(len(excess)), excess, color=color, linewidth=1.8)
    ax.fill_between(range(len(excess)), excess, 0, color=color, alpha=fill_alpha)
    ax.axhline(0, color=COLOR_MUTED, linewidth=0.5, alpha=0.4)

    # Strip chrome
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.margins(x=0.02)

    # Tiny label showing final value
    ax.text(
        len(excess) - 1, end_excess,
        f"  {end_excess * 100:+.1f}%",
        color=color,
        fontsize=8,
        verticalalignment="center",
        fontweight="bold",
    )

    return _fig_to_data_uri(fig, dpi=100)


# ---------------------------------------------------------------------------
# Chart 2: per-trade price chart (daily flagged trade cards)
# ---------------------------------------------------------------------------

def price_chart_since_trade(
    ticker: str,
    trade_date: str,
    width_px: int = 560,
    height_px: int = 220,
) -> Optional[str]:
    """
    Price chart showing ticker's cumulative % return since trade_date
    with SPY benchmark overlay. Entry point (day 0, 0%) is explicitly
    marked. Suitable for per-trade cards in daily digest.

    Returns base64 data URI or None.
    """
    if not HAS_MATPLOTLIB:
        return None

    stock_series = _get_price_series_since(ticker, trade_date)
    spy_series = _get_price_series_since("SPY", trade_date)
    if not stock_series or not spy_series or len(stock_series) < 3:
        return None

    # Align by date
    spy_by_date = {p["date"]: p["close"] for p in spy_series}
    aligned_stock: List[Dict] = []
    aligned_spy: List[Dict] = []
    for p in stock_series:
        if p["date"] in spy_by_date:
            aligned_stock.append(p)
            aligned_spy.append({"date": p["date"], "close": spy_by_date[p["date"]]})
    if len(aligned_stock) < 3:
        return None

    stock_cum = [x * 100 for x in _cumulative_pct_move(aligned_stock)]  # to %
    spy_cum = [x * 100 for x in _cumulative_pct_move(aligned_spy)]

    x = list(range(len(stock_cum)))
    final_stock = stock_cum[-1]
    final_spy = spy_cum[-1]
    final_excess = final_stock - final_spy
    stock_color = COLOR_SUCCESS if final_excess >= 0 else COLOR_ERROR

    fig, ax = plt.subplots(
        figsize=(width_px / 100, height_px / 100),
        dpi=100,
        facecolor=COLOR_BG,
    )
    ax.set_facecolor(COLOR_BG)

    ax.plot(x, stock_cum, color=stock_color, linewidth=2.0, label=ticker)
    ax.plot(x, spy_cum, color=COLOR_MUTED, linewidth=1.3, label="SPY", linestyle="--", alpha=0.75)
    ax.fill_between(
        x, stock_cum, spy_cum,
        where=[s >= p for s, p in zip(stock_cum, spy_cum)],
        color=COLOR_SUCCESS, alpha=0.12, interpolate=True,
    )
    ax.fill_between(
        x, stock_cum, spy_cum,
        where=[s < p for s, p in zip(stock_cum, spy_cum)],
        color=COLOR_ERROR, alpha=0.12, interpolate=True,
    )

    # Entry marker
    ax.scatter([0], [0], color=COLOR_ACCENT, s=40, zorder=5, edgecolor="white", linewidth=1)
    ax.axhline(0, color=COLOR_BORDER, linewidth=0.8, alpha=0.6)

    # Final value labels
    ax.text(
        x[-1], final_stock,
        f"  {ticker} {final_stock:+.1f}%",
        color=stock_color, fontweight="bold", fontsize=9, verticalalignment="center",
    )
    ax.text(
        x[-1], final_spy,
        f"  SPY {final_spy:+.1f}%",
        color=COLOR_MUTED, fontsize=8, verticalalignment="center",
    )

    # Clean chrome
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(COLOR_BORDER)
    ax.spines["bottom"].set_color(COLOR_BORDER)
    ax.tick_params(colors=COLOR_MUTED, labelsize=8)
    ax.set_xlabel(f"Trading days since trade ({trade_date})", color=COLOR_MUTED, fontsize=8)
    ax.set_ylabel("Cumulative % return", color=COLOR_MUTED, fontsize=8)
    ax.grid(True, color=COLOR_BORDER, alpha=0.25, linewidth=0.5)

    # Legend
    ax.legend(
        loc="upper left",
        framealpha=0,
        labelcolor=COLOR_TEXT,
        fontsize=8,
    )

    return _fig_to_data_uri(fig, dpi=100)


# ---------------------------------------------------------------------------
# Chart 3: tier breakdown donut (weekly)
# ---------------------------------------------------------------------------

def tier_donut(
    counts: Dict[str, int],
    width_px: int = 340,
    height_px: int = 340,
) -> Optional[str]:
    """
    Donut chart showing the distribution of tiers in the week.
    counts: {STRONG: int, BASE: int, MODERATE: int, SKIP: int}
    """
    if not HAS_MATPLOTLIB:
        return None

    tiers = ["STRONG", "BASE", "MODERATE", "SKIP"]
    values = [counts.get(t, 0) for t in tiers]
    if sum(values) == 0:
        return None

    colors = [TIER_COLORS[t] for t in tiers]

    fig, ax = plt.subplots(
        figsize=(width_px / 100, height_px / 100),
        dpi=100,
        facecolor=COLOR_BG,
    )
    ax.set_facecolor(COLOR_BG)

    # Only label non-zero slices
    labels = [
        f"{t}\n{v}" if v > 0 else ""
        for t, v in zip(tiers, values)
    ]

    wedges, texts = ax.pie(
        values,
        colors=colors,
        labels=labels,
        startangle=90,
        counterclock=False,
        wedgeprops={"width": 0.40, "edgecolor": COLOR_BG, "linewidth": 2},
        textprops={"color": COLOR_TEXT, "fontsize": 10, "fontweight": "bold"},
    )

    # Center total
    total = sum(values)
    ax.text(0, 0.05, f"{total}", ha="center", va="center",
            color=COLOR_TEXT, fontsize=22, fontweight="bold")
    ax.text(0, -0.18, "trades", ha="center", va="center",
            color=COLOR_MUTED, fontsize=10)

    ax.set_aspect("equal")
    return _fig_to_data_uri(fig, dpi=100)


# ---------------------------------------------------------------------------
# Chart 4: winners/losers bar chart (weekly)
# ---------------------------------------------------------------------------

def winners_losers_bar(
    winners: List[Dict],
    losers: List[Dict],
    width_px: int = 580,
    height_px: int = 340,
) -> Optional[str]:
    """
    Horizontal bar chart combining top 5 winners + top 5 losers by
    excess vs SPY.

    Each row expected: {ticker, politician, excess (decimal fraction)}
    """
    if not HAS_MATPLOTLIB:
        return None

    rows = []
    # Winners first (best at top)
    for w in (winners or [])[:5]:
        rows.append({
            "label": f"{w.get('ticker', '—')} · {(w.get('politician') or '')[:16]}",
            "excess": (w.get("excess") or 0) * 100,
            "is_winner": True,
        })
    # Losers (worst at bottom)
    for l in (losers or [])[:5]:
        rows.append({
            "label": f"{l.get('ticker', '—')} · {(l.get('politician') or '')[:16]}",
            "excess": (l.get("excess") or 0) * 100,
            "is_winner": False,
        })
    if not rows:
        return None

    # Reverse so largest-absolute bars are at top (matplotlib barh stacks bottom-to-top)
    rows = list(reversed(rows))

    labels = [r["label"] for r in rows]
    values = [r["excess"] for r in rows]
    colors = [COLOR_SUCCESS if v >= 0 else COLOR_ERROR for v in values]

    fig, ax = plt.subplots(
        figsize=(width_px / 100, height_px / 100),
        dpi=100,
        facecolor=COLOR_BG,
    )
    ax.set_facecolor(COLOR_BG)

    bars = ax.barh(labels, values, color=colors, edgecolor="none", height=0.65)

    # Value labels on bars
    for bar, val in zip(bars, values):
        bar_width = bar.get_width()
        ha = "left" if bar_width >= 0 else "right"
        offset = 0.3 if bar_width >= 0 else -0.3
        ax.text(
            bar_width + offset,
            bar.get_y() + bar.get_height() / 2,
            f"{val:+.1f}%",
            va="center", ha=ha,
            color=COLOR_TEXT,
            fontsize=8, fontweight="bold",
        )

    ax.axvline(0, color=COLOR_BORDER, linewidth=0.8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(COLOR_BORDER)
    ax.spines["bottom"].set_color(COLOR_BORDER)
    ax.tick_params(colors=COLOR_MUTED, labelsize=8)
    ax.set_xlabel("Excess return vs SPY (%)", color=COLOR_MUTED, fontsize=9)
    ax.set_title(
        "Week's winners and losers",
        color=COLOR_TEXT, fontsize=11, fontweight="bold", loc="left",
    )
    ax.grid(True, axis="x", color=COLOR_BORDER, alpha=0.25, linewidth=0.5)

    return _fig_to_data_uri(fig, dpi=100)


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not HAS_MATPLOTLIB:
        print("matplotlib not installed — run: pip3 install --user matplotlib",
              file=sys.stderr)
        sys.exit(1)

    out_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "outputs", "tmp",
    )
    os.makedirs(out_dir, exist_ok=True)

    def _save_to_file(data_uri: str, path: str) -> None:
        """Decode a data URI and write to disk for visual inspection."""
        if not data_uri:
            print(f"  SKIP {path} (no data)")
            return
        import re
        m = re.match(r"data:image/png;base64,(.+)", data_uri)
        if not m:
            print(f"  SKIP {path} (malformed URI)")
            return
        with open(path, "wb") as f:
            f.write(base64.b64decode(m.group(1)))
        print(f"  wrote {path} ({len(data_uri)} chars URI)")

    print("Generating test charts...")

    print("\n1. Sparkline for NVDA since 2026-01-15:")
    uri = sparkline_cumulative_excess("NVDA", "2026-01-15")
    _save_to_file(uri, os.path.join(out_dir, "chart_sparkline_NVDA.png"))

    print("\n2. Per-trade price chart for MSFT since 2025-11-18 (Cisneros):")
    uri = price_chart_since_trade("MSFT", "2025-11-18")
    _save_to_file(uri, os.path.join(out_dir, "chart_trade_MSFT.png"))

    print("\n3. Tier donut:")
    uri = tier_donut({"STRONG": 1, "BASE": 4, "MODERATE": 12, "SKIP": 3})
    _save_to_file(uri, os.path.join(out_dir, "chart_tier_donut.png"))

    print("\n4. Winners/losers bar:")
    uri = winners_losers_bar(
        winners=[
            {"ticker": "HOG", "politician": "Tim Moore", "excess": 0.177},
            {"ticker": "CBRL", "politician": "Tim Moore", "excess": 0.095},
            {"ticker": "LGIH", "politician": "Tim Moore", "excess": 0.119},
        ],
        losers=[
            {"ticker": "SMPL", "politician": "Tim Moore", "excess": -0.207},
            {"ticker": "MSFT", "politician": "Gilbert Cisneros", "excess": -0.246},
        ],
    )
    _save_to_file(uri, os.path.join(out_dir, "chart_winners_losers.png"))

    backtest.save_price_cache()
    print(f"\nCharts saved to {out_dir} — open them to visually verify.")
