# 08 — Open Items & Deferred

## Open Items (lock before build)

1. **Distribution list** — who gets daily digest, STRONG alerts, weekly report?
2. **Scheduling mechanism** — cron on DigitalOcean droplet, Claude scheduled-tasks, or manual?
3. **Phase 0 timeline** — days/weeks to trusted roster? Address in implementation plan.

## Deferred to Future Phases

**Sell-signal pipeline** — Parallel pipeline for politician sells → bearish plays (puts, put spreads). Deferred: sells are noisy (tax-loss harvesting, rebalancing, liquidity, divorce). Higher evidence bar needed. Revisit after 3+ months of buy-side paper-trading data validates the scoring framework.

**Position sizing / portfolio-level risk** — Sector concentration caps, correlation checks between open positions, max concurrent positions. Revisit once paper-trading ledger has enough data to analyze portfolio-level patterns.

## Key Architecture Decisions

- Committees: 4 → **8 primary + 2 watchlist + leadership overlay**
- Committee alignment: binary gate → **multiplier system with mega-cap override list**
- OWD thresholds: fixed percentages → **stock-normalized using per-stock vol/range/implied moves**
- All scoring params: hardcoded → **tunable constants with 5-step feedback loop**
- Backtest: flat 5yr → **recency-weighted + committee transition flags + auto re-validation**
- Spouse trades: unhandled → **tagged, media-penalized, tier-capped, separately tracked**
- Clustering window: 14 days → **30 days (matches STOCK Act disclosure lag)**
- Options builder: separate agent → **shared code module**
- Storage: JSON ledgers → **SQLite with diagnostics + parameter changelog tables**
