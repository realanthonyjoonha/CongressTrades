# 01 — Universe: Committees, Roster, Mapping

## Committees (8 primary + 2 watchlist)

| # | Committee | House | Senate | Signal domain |
|---|---|---|---|---|
| 1 | Intelligence | HPSCI | SSCI | Geopolitics, cyber, defense, China/Taiwan, semis, AI export controls |
| 2 | Armed Services | HASC | SASC | DoD contracts, weapons, defense budget, defense primes, military AI |
| 3 | Energy & Commerce / ENR | E&C | ENR | FDA, drug pricing, energy, pipelines, telecom, AI regulation, data privacy, social media |
| 4 | Appropriations / Finance / W&M | Approp | Finance, W&M | Federal spending, tax policy, Treasury — broad cross-sector |
| 5 | Foreign Affairs | HFAC | SFRC | Sanctions, export controls, arms sales, geopolitical risk, trade agreements |
| 6 | Commerce, Science & Transport | — | Senate Commerce | AI policy, FTC, NIST, broadband, platform regulation |
| 7 | Banking / Financial Services | HFSC | Senate Banking | Bank regulation, crypto, Fed oversight, financial stability |
| 8 | Homeland Security | HHS | HSGAC | CISA, DHS contracting, cybersecurity legislation, border tech |

**Watchlist-tier** (0.5x alignment unless clustering elevates):
- **Judiciary** — Big Tech antitrust, AI copyright/IP, DOJ investigations
- **Science, Space & Technology** — NASA contracts, NSF/NIST, R&D, quantum

## Leadership Overlay

| Position type | Scope | Alignment floor |
|---|---|---|
| Speaker, Majority/Minority Leader, Whip, Caucus Chair | Chamber-wide — all trades | 0.7x on ALL trades |
| Committee Chair, Ranking Member | Committee-scoped | 0.7x only within own committee's jurisdiction |

All leadership holders auto-included in backtest candidate universe regardless of committee.

## Sector ↔ Committee Mapping

### Level 1 — Sector defaults

| Sector | Maps to |
|---|---|
| Semiconductors / AI / cyber / China-exposed tech | HPSCI, Senate Commerce |
| Defense primes (LMT, RTX, NOC, GD, etc.) | HASC |
| Biotech / pharma / medical devices | E&C |
| Oil & gas / pipelines / utilities / renewables | E&C / ENR |
| Telecom | E&C, Senate Commerce |
| Broad-market / financials / tax-sensitive | Appropriations / Finance / W&M |
| Banks / crypto / financial regulation | Banking / Financial Services |
| Geopolitically exposed (sanctions, export controls) | Foreign Affairs |
| Cybersecurity / border tech / DHS contractors | Homeland Security |
| Big Tech antitrust targets | Judiciary |
| AI infrastructure / cloud / data centers | Senate Commerce, E&C |

### Level 2 — Mega-cap override list

Tickers touching 3+ committee jurisdictions — ANY roster member's trade proceeds to Stage 2+:

```
MSFT, AAPL, GOOGL, AMZN, META, NVDA, TSMC, AVGO, CRM, ORCL, IBM,
LMT, RTX, BA, GD, NOC, JPM, GS, UNH, JNJ, PFE, CVX, XOM, CRWD, PANW, PLTR
```

Each override ticker stored in `committee_mappings` table with `multi_jurisdiction` flag and per-committee rationale. List refined during Phase 0 backtest. Version-controlled.

## Roster Generation (Backtest Agent — Phase 0)

**Candidate universe**: All current members of the 8 primary committees + all leadership holders. ~200–250 people.

**Lookback**: 5 years of disclosed trades.

**Filter**: Only committee-aligned trades (ticker sector maps to a committee the member sat on at trade date — committee membership is time-varying).

**Two metrics per candidate**:
1. Overall committee-aligned hit rate + return vs SPY at 60-day horizon (full 5yr)
2. Recency-weighted: 0–12mo = 1.0x, 12–24mo = 0.7x, 24–36mo = 0.5x, 36–48mo = 0.3x, 48–60mo = 0.15x

**Pass bar**: Both metrics must independently show >5% vs SPY AND >55% hit rate.
- Passes both → **core** tier
- Passes overall but fails recency → **"fading edge"** → watchlist tier
- **Floor**: If <15 pass, take top 15 by committee-aligned hit rate. Below-bar members flagged as watchlist.

**Committee transition flag**: Changed committees in last 18 months → `committee-transition` flag. Old committee performance doesn't count. Probationary status (max MODERATE signals) until 5 committee-aligned trades on new committee OR 12 months, whichever first.

**Re-validation trigger**: Trailing 6-month hit rate drops below 45% with ≥5 trades → demote to watchlist. <5 trades → flag "low activity" in weekly report, no demotion.

**Re-run schedule**: At project start (Phase 0), on congressional session changes, committee reshuffles, quarterly recalibration, or on-demand.
