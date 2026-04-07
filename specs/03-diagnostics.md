# 03 — Opportunity Window Diagnostic (Stage 2 Detail)

All thresholds are **stock-normalized**. No fixed percentage cutoffs. Each check uses a tunable constant (`k_XX`) refined by the feedback loop (`specs/04-feedback.md`).

## Required Per-Stock Data

Computed at evaluation time by Agent 1 (cached daily):

| Metric | Formula | Source |
|---|---|---|
| `hist_range_45d` | Median absolute price move over rolling 45-day windows, trailing 1yr | yfinance |
| `realized_vol_60d` | 60-day annualized realized volatility | yfinance |
| `sigma_move(N)` | `price × (realized_vol_60d / √252) × √N` | derived |
| `iv_range_12m` | Max IV percentile − min IV percentile, trailing 12mo | MMD |
| `implied_earnings_move` | Pre-earnings weekly ATM straddle price as % of underlying | MMD options chain |
| `sector_vol_60d` | 60-day annualized realized vol of ticker's sector ETF | yfinance |

## Dimension A — Entry Quality

| Check | Fires when | Default | Pts |
|---|---|---|---|
| **A1. Range consumption** | Directional move since trade > `k_A1 × hist_range_45d` | `k_A1 = 0.60` | +1 |
| **A2. Sigma exhaustion** | Directional move since trade > `k_A2 × sigma_move(days_elapsed)` AND RSI > 70 (buy) / < 30 (sell) | `k_A2 = 1.50` | +1 |
| **A3. IV expansion** | IV percentile > 70 AND IV expanded > `k_A3 × iv_range_12m` since trade date | `k_A3 = 0.40` | +1 |
| **A4. Volume front-run** | Any day in `k_A4_window` days post-trade had volume > `k_A4_mult × 20d avg` | `k_A4_window = 5`, `k_A4_mult = 2.0` | +1 |

## Dimension B — Catalyst Status

| Check | Fires when | Default | Pts |
|---|---|---|---|
| **B1. Earnings pass-through** | Earnings occurred since trade AND actual move > `k_B1 × implied_earnings_move` | `k_B1 = 1.0` | +2 |
| **B2. Legislative/regulatory event** | Material event (markup, vote, FDA decision, FERC ruling, contract award) occurred since trade. Binary — not vol-normalized. Routine hearings do NOT count. | n/a | +2 |
| **B3. News absorption** | Major catalyst fired AND stock moved > `k_B3 × sigma_move(5)` on news day(s) | `k_B3 = 1.0` | +1 |
| **B4. Sector rotation** | Sector ETF moved > `k_B4 × sector_vol_60d / √252 × √days_elapsed` | `k_B4 = 1.5` | +1 |

B1/B2 carry **double weight** — once insider edge is consumed, entry price doesn't save the thesis.

## Combined Scoring

| Points | Verdict |
|---|---|
| 0 – `threshold_open` (default 1) | **Window open** → Stage 3 |
| `threshold_open+1` – `threshold_narrowing` (default 2–3) | **Window narrowing** → proceed, flag "late entry, consider reduced size or spread" |
| > `threshold_narrowing` (default 4+) | **Window closed** → log `opportunity-expired`, stop |

A single B1 or B2 (2pts) + any A check (1pt) = 3pts minimum → window narrowing. Two catalyst checks + one A check = 5pts → closed. Intentional.

## Normalization Examples

| Stock type | `hist_range_45d` | A1 triggers at | A2 (1.5σ, 30 days) triggers at |
|---|---|---|---|
| Utility (vol ~15%) | ~3.5% | ~2.1% | ~4.1% |
| Large-cap tech (vol ~25%) | ~6% | ~3.6% | ~6.8% |
| High-beta tech (vol ~60%) | ~15% | ~9.0% | ~16.4% |
| Biotech (vol ~80%) | ~20% | ~12.0% | ~21.8% |

10% move closes window on utility, barely registers on biotech. Correct behavior.

## Full Parameter Reference

| Constant | Description | Default | Review cadence |
|---|---|---|---|
| `k_A1` | Range consumption fraction | 0.60 | Weekly |
| `k_A2` | Sigma exhaustion multiplier | 1.50 | Weekly |
| `k_A3` | IV range fraction | 0.40 | Weekly |
| `k_A4_window` | Volume check window (days) | 5 | Quarterly |
| `k_A4_mult` | Volume spike multiplier | 2.0 | Weekly |
| `k_B1` | Implied earnings move multiplier | 1.0 | Weekly |
| `k_B3` | News absorption sigma multiplier | 1.0 | Weekly |
| `k_B4` | Sector rotation sigma multiplier | 1.5 | Weekly |
| `k_cluster_window` | Clustering lookback (days) | 30 | Quarterly |
| `threshold_open` | Max pts for "window open" | 1 | Quarterly |
| `threshold_narrowing` | Max pts for "window narrowing" | 3 | Quarterly |
| `k_fn_threshold` | False negative rate alert trigger | 0.30 | Quarterly |
| `k_fp_threshold` | False positive rate alert trigger | 0.40 | Quarterly |

All defaults are hypotheses. Refined via feedback loop — see `specs/04-feedback.md`.
