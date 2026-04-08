# Phase 2.3.5 — Real Options Integration (yfinance + Black-Scholes + thin MMD client)

## Context

Phase 2.3 shipped the Daily Signal agent with **conceptual** options recommendations: per-tier delta target bands and DTE windows in prose, no specific strikes, no Greeks. The plan committed to a follow-up phase (2.3.5) that would upgrade STRONG-tier plays to **real strikes with real Greeks** via MMD's options chain snapshot endpoint.

When the build started, the snapshot endpoint returned `403 not entitled` on the current MMD subscription tier. The accessible MMD endpoints are:

- ✅ Stock OHLC aggregate bars (`/v2/aggs/ticker/{ticker}/range/...`)
- ✅ Options reference / contract listings (`/v3/reference/options/contracts`)
- ✅ Options OHLC aggregate bars (`/v2/aggs/ticker/O:{contract}/...`)
- ✅ Stock metadata (`/v3/reference/tickers/{ticker}`)
- ❌ Options chain snapshot (`/v3/snapshot/options/{ticker}`)
- ❌ Earnings calendar (`/benzinga/v1/earnings`)
- ❌ Corporate events (`/tmx/v1/corporate-events`)
- ❌ Financial statements

The original plan would have required upgrading the MMD subscription. After surfacing the constraint to the user and discussing alternatives, we decided to **pivot to yfinance** as the primary data source. yfinance has options chains, earnings calendar, and is free. The result is the same scope (real Greeks for STRONG plays, earnings-driven DTE) without subscription dependencies.

## Goals

1. Replace conceptual options with **real strikes** (specific contract symbol) for STRONG and BASE pre-tier trades
2. Compute **Greeks** (delta, gamma, theta, vega) from chain IV via Black-Scholes
3. **Earnings-aware DTE selection** — pull next earnings date from yfinance and use it as the catalyst driver
4. Build a **thin MMD client** for the endpoints that DO work on the current tier, as a future-proofing hook for when the user upgrades or for Phase 3 historical-options backtesting

## Scope

### IN scope

**Black-Scholes module (`scripts/bs_greeks.py`)**
- Pure Python (no scipy/numpy), uses `math.erf` for normal CDF
- `bs_price`, `bs_delta`, `bs_gamma`, `bs_theta` (per day), `bs_vega` (per 1% IV)
- `bs_all_greeks` convenience that returns all Greeks + price + IV in one call
- `implied_vol_from_price` — inverse BS via bisection (used to compute IV from yfinance lastPrice when yfinance's IV field is unreliable)
- `pick_strike_by_delta` — given a chain + target delta, pick the strike whose computed delta is closest

**Real options chain layer (`scripts/options_chain.py`)**
- `list_expirations(ticker)` — yfinance Ticker.options
- `fetch_chain(ticker, expiration, option_type, spot, risk_free_rate)` — yfinance chain with sanity filters and **inverse-BS IV computation** to handle off-hours data
- `pick_expiration(expirations, target_dte_min, target_dte_max, today)` — chooses the expiry inside the catalyst-driven DTE window
- `target_dte_range(signal_tier, has_catalyst, catalyst_date, today, structural)` — derives DTE window from tier + catalyst (catalyst date + 14d buffer, or 6-12 weeks default, or 12-18mo for LEAPS)
- `best_strike_for_trade(...)` — orchestrator that returns either a `mode: "real"` dict with strike/expiry/Greeks/contract, or falls back to `mode: "conceptual"` (the old `options_concept.py` output) on failure

**MMD client (`scripts/mmd_client.py`)**
- Pure Python with urllib (no `requests` dep)
- Resolves API key from `MMD_API_KEY` env var or `config/mmd.json`
- `get_stock_aggregates(ticker, from_date, to_date)` — daily OHLC bars
- `list_option_contracts(underlying, expiration_gte, expiration_lte, strike_gte, strike_lte)` — contract listings
- `get_option_aggregates(option_ticker, from_date, to_date)` — historical OHLC for a specific option contract
- `get_option_prev_close(option_ticker)` — previous-day close for a contract
- `get_ticker_reference(ticker)` — stock metadata
- `capability_check()` — probes each endpoint family and returns `{endpoint: bool}` so the user can diagnose subscription tier coverage
- Raises `MMDAuthError` on 403 so callers can catch entitlement issues distinctly from network errors

**Earnings calendar in `stock_metrics.py`**
- New `get_next_earnings(ticker)` — fetches `Ticker.calendar` from yfinance and returns `{next_earnings_date, eps_avg, eps_high, eps_low, revenue_avg}` or None
- `compute_metrics_for_trade` now populates `next_earnings_date` and EPS estimate fields in the metrics dict
- Uses `Ticker.calendar` (NOT `earnings_dates` which requires `lxml`)

**Daily Signal Phase A driver upgrade (`scripts/daily_signal.py`)**
- Research pack now includes a "**Real-chain options pick**" subsection per STRONG/BASE trade with strike, expiry, delta/gamma/theta/vega, IV, contract symbol, snapshot caveat
- Falls back to a conceptual line when real chain is unavailable (off-hours data quality, ticker has no options, etc.)
- Earnings date surfaces under "**Next earnings**" with EPS estimate range, marked for Stage 3 catalyst use

**LLM prompt updates (`prompts/daily_signal.md`)**
- Explicit "do not invent strikes or Greeks — use the values verbatim from the research pack's Real-chain options pick block"
- Earnings calendar usage guidance (use the pre-fetched date as primary forward catalyst)
- Methodology section updated to reflect Phase 2.3.5 architecture

### OUT of scope (deferred)

- **A3 IV expansion check** — needs a historical IV cache built up over months. Phase 3 work where the Data Maintenance agent fetches ATM IV daily and writes to `stock_metrics`.
- **B1 earnings pass-through (textbook formula)** — needs implied earnings move which requires historical chain snapshots. Substituted by Stage 3 catalyst identification (earnings date in metrics → LLM uses it).
- **Real-Greeks via MMD's snapshot endpoint** — requires subscription upgrade. The thin MMD client establishes the integration pattern; when the user upgrades, swap `options_chain.fetch_chain` to call `mmd_client.get_option_chain_snapshot()` (one-line backend swap).
- **Historical options backfill** — MMD's options aggregate bars work on the current tier and could be used to backfill historical IVs and to retroactively score recommendations. Deferred to Phase 3 feedback loop.
- **Risk-free rate from current treasury yield** — currently hard-coded at 4.5%. Could fetch from yfinance `^TNX` or MMD treasury yields endpoint. Tiny impact on Greeks, deferred.
- **Spread / multi-leg structures** (debit spread alternative when IV is high) — flagged with `iv_warning` text but no actual spread is constructed. Phase 3+.
- **Open interest filter improvements** — yfinance returns OI=0 for most strikes (probably a yfinance bug), which forces us to rely on volume only. Could cross-validate with MMD's historical aggregates. Deferred.

## Architecture

```
daily_signal.py (Phase A)
    │
    ├─ pipeline.score_trade()              [Stages 1, 2, 4]
    ├─ stock_metrics.compute_metrics_for_trade()
    │     ├─ yfinance: hist_range, vol, RSI, volume, sector ETF
    │     └─ yfinance: NEW — earnings calendar (next date + EPS estimates)
    │
    ├─ select_for_llm(top N by alignment × cluster)
    │
    └─ FOR EACH STRONG/BASE selected trade:
          options_chain.best_strike_for_trade(ticker, tier, catalyst=earnings_date)
              │
              ├─ get_spot_price(ticker)              [backtest._price_cache]
              ├─ list_expirations(ticker)            [yfinance Ticker.options]
              ├─ pick_expiration(target_dte)         [DTE-window-aware]
              ├─ fetch_chain(ticker, expiry, spot)
              │     ├─ yfinance Ticker.option_chain()
              │     ├─ Filter: lastPrice > 0, vol > 0
              │     ├─ Inverse BS: compute IV from lastPrice  ← key trick
              │     └─ Filter: 0.10 ≤ IV ≤ 2.0
              ├─ pick_strike_by_delta(target=0.45)   [bs_greeks]
              │     ├─ bs_delta() per strike
              │     └─ Pick lowest |delta - target|
              └─ Returns dict with strike + Greeks + contract symbol

         (output threaded into research pack as "Real-chain options pick")

    │
    ▼
Phase B: claude --print on prompts/daily_signal.md
    └─ LLM uses the real-chain block verbatim in STRONG/BASE narrative sections

    │
    ▼
Phase C: writeback (existing) + format/email (existing)
```

## The "inverse BS for IV" trick

**Why this matters:** yfinance's `impliedVolatility` field returns garbage during off-hours — we observed it returning placeholder values like 0.0001, 0.0078, 0.0313, 0.0625, 0.125, 0.25 (powers-of-2 fractions, not real IV). Out of 12 strikes for AAPL on a Tuesday at 02:30 ET, every IV was one of these placeholder values.

**The fix:** ignore yfinance's IV entirely. yfinance's `lastPrice` IS reliable (it's the prior trading day's close). For each strike:

1. Take `lastPrice` as the option's market price
2. Solve Black-Scholes inverse: given price + spot + strike + expiry + risk-free rate, find IV via bisection
3. Use the computed IV to forward-compute delta/gamma/theta/vega
4. Validate: BS price at the computed IV should equal lastPrice (round-trip check)

This makes the system robust to off-hours data quality issues. Whether the agent runs at 5 AM ET (pre-market) or 5 PM ET (post-close), the strikes have valid IVs.

## Files shipped

| File | Status | Notes |
|---|---|---|
| `scripts/bs_greeks.py` | NEW (~280 lines) | Pure-Python Black-Scholes. delta/gamma/theta/vega/price + inverse BS for IV + strike picker. No scipy/numpy. |
| `scripts/options_chain.py` | NEW (~410 lines) | yfinance chain fetcher + sanity filters + inverse-BS IV + DTE-window expiry picker + best_strike_for_trade orchestrator. Falls back to conceptual on failure. |
| `scripts/mmd_client.py` | NEW (~340 lines) | Thin Python wrapper for MMD endpoints that work on current tier. Stock + options aggregates, contract listings, capability_check. Distinct MMDAuthError for 403s. |
| `scripts/stock_metrics.py` | MODIFIED | New `get_next_earnings(ticker)` helper. `compute_metrics_for_trade` now populates `next_earnings_date` + EPS estimates. |
| `scripts/daily_signal.py` | MODIFIED | Research pack renderer adds "**Real-chain options pick**" subsection per STRONG/BASE trade and "**Next earnings**" subsection. Calls `options_chain.best_strike_for_trade` per selected trade. |
| `prompts/daily_signal.md` | MODIFIED | Constraints updated: "do not invent strikes or Greeks — use the real-chain values verbatim". Earnings calendar guidance. Methodology section rewritten. |
| `docs/plans/phase-2-3-5-real-options.md` | NEW | This document. |

## CLI

```bash
# Standalone Black-Scholes smoke test
python3 scripts/bs_greeks.py
# Prints 4 cases (ATM, OTM, LEAPS, strike picker) + IV round-trip test

# Standalone options chain test (5 cases)
python3 scripts/options_chain.py
# NVDA STRONG with catalyst, MSFT STRONG no catalyst, AAPL BASE,
# GOOGL MODERATE, XYZABC fallback test

# Standalone earnings test
python3 scripts/stock_metrics.py NVDA semiconductors 2025-08-01
# Returns full metrics dict including next_earnings_date

# MMD capability probe (requires MMD_API_KEY env var or config/mmd.json)
python3 scripts/mmd_client.py capability
# Returns {stock_aggregates: True/False, option_contracts: True/False,
#          option_snapshot: True/False, earnings_benzinga: True/False}

# Full Phase A run (existing CLI, now with real strikes)
./run-agent.sh daily --dry-run
```

## Smoke test results

1. **Black-Scholes accuracy:** ATM NVDA call (180 strike, 30 DTE, 32% IV) → price 5.94, delta 0.49, theta -0.12/day. Matches textbook values. IV round-trip recovers 0.32 to 4 decimal places.

2. **Strike picker:** target delta 0.45, picked 182.5 strike with delta 0.43 (error 0.018). Correct.

3. **NVDA real chain:** spot $177.64, picked strike $186 expiry 2026-06-18 (71 DTE), delta 0.45, IV 38.6%, gamma 0.013, theta -0.093. NVDA IV ~38% is realistic for high-vol AI name.

4. **MSFT real chain:** spot $372.88, picked strike $385 expiry 2026-06-18 (71 DTE), delta 0.46, IV 30.6%. MSFT IV ~30% is realistic for mega-cap.

5. **AAPL real chain (with catalyst):** spot $258.86, earnings date 2026-04-29, picked strike $265 expiry 2026-05-15 (37 DTE — covers earnings + 14d buffer), delta 0.41, IV 22.3%. **DTE was driven by the earnings calendar** — this is the Phase 2.3.5 win.

6. **MMD client CSV parser:** 5/5 unit tests pass (parse aggregates, parse contracts, empty body, header only, blank-line-stops-parser).

7. **Earnings calendar:** NVDA next earnings 2026-05-20, EPS avg $1.78 (range $1.69–$1.99). Populates correctly into stock_metrics dict.

8. **Daily Signal Phase A end-to-end:** ran on 621 trades with `--limit-trades 2`, scored in 1m9s, research pack 57KB. Cisneros MSFT shows real-chain pick (call 380 May 15, delta 0.47, IV 34.3%, gamma 0.0098, theta -0.239, contract MSFT260515C00380000). DTE was driven by MSFT's earnings date 2026-04-29 from yfinance calendar. **The catalyst-aware DTE selection works end-to-end.**

## Known limitations

- **Off-hours bid/ask are stale.** yfinance returns bid=0/ask=0 outside market hours. The picked strike's `bid` and `ask` fields may be 0 in pre-market runs. The `lastPrice` field is reliable. The snapshot caveat in the research pack instructs the user to verify bid/ask before entry.
- **Open interest is unreliable.** yfinance returns OI=0 for most strikes regardless of true OI. We rely on `volume > 0` as a liquidity gate.
- **A3 IV expansion still skipped.** Requires historical IV cache (Phase 3).
- **Risk-free rate is hard-coded** at 4.5%. Tiny impact on Greeks.
- **No multi-leg structures.** Single-strike calls/puts only. Spreads are mentioned in `iv_warning` text but not constructed.

## Known cascades

- **`bs_greeks` module is fully reusable** by Phase 3 feedback loop for retroactively scoring historical recommendations
- **`mmd_client` capability check** is a permanent diagnostic — when the user upgrades MMD, just run `python3 scripts/mmd_client.py capability` to see what newly unlocked
- **The inverse-BS-IV trick** can be reused if MMD's snapshot endpoint becomes available — same logic applies if MMD's IV is also unreliable
- **The earnings date enrichment** is now part of the standard metrics dict, so any future agent that calls `compute_metrics_for_trade` automatically gets earnings context for free

## Outstanding for Phase 2.3.5

- **Live full E2E with claude --print** — pending user manual run of `./run-agent.sh daily` to verify the LLM correctly uses the real-chain block in its STRONG/BASE narrative
- **Verify MMD client works** with the user's API key — set `MMD_API_KEY` env var or create `config/mmd.json` and run `python3 scripts/mmd_client.py capability`. The client itself is only used by Phase 3 work, not Phase 2.3.5's daily flow.
- **Integrate `options_chain` into `weekly_deep.py`** — currently only daily_signal uses it. The weekly retrospective could also show "the real-chain pick from Monday's daily, where is it now?" Phase 2.3.5 doesn't include this — it's a small follow-up.
