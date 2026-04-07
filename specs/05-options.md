# 05 — Options Layer & Paper Trading

## Options Plays by Signal Tier

### STRONG → Real Greeks, Executable Trades
- Call MMD options chain endpoint, pull live chain
- Strike selection by sub-tier:
  - **Base**: delta 0.40–0.60
  - **Aggressive**: delta 0.25–0.35
  - **LEAPS**: delta 0.70+
- Return real bid/ask, delta, gamma, theta, vega, IV
- Timestamp caveat: *"snapshot at HH:MM ET — verify before entry"*
- Paper ledger uses timestamped entry price

### BASE / MODERATE → Conceptual Plays
- Describe structure: DTE window, delta target, tier (aggressive/base/LEAPS)
- No specific strikes — reader does final broker step
- Lighter-weight, safer against stale data

## DTE Selection — Catalyst-Driven
- Expiry covers identified forward catalyst + 2 week buffer
- No catalyst identified → default 6–12 weeks (BASE/MODERATE)

## IV Discipline — Override, Not Veto
- IV percentile > 70 → play still recommended
- Report flags: "expensive IV — consider debit spread alternative or wait"
- Does NOT block recommendation

## Bear Case Narrative — Always Included
- Narrative discussing what would invalidate the thesis
- No explicit stop-loss — risk management is reader's job

## Options Module
Shared code, not a separate agent. Called by Agent 2 (daily) and Agent 3 (weekly).

## Paper-Trading Layer

Every recommended play becomes a simulated position:

| Aspect | STRONG | BASE/MODERATE |
|---|---|---|
| Entry price | Mid/mark at recommendation timestamp | Same |
| Mark-to-market | Daily via MMD live chain | Synthetic Black-Scholes from underlying + IV |
| Auto-close | On expiry or bear case fires | Same |

**P&L rollups by**: signal tier, politician, committee type, sector, catalyst type, `member_direct` vs `spouse`.

Over 6–12 months this becomes the **edge database** — empirical data feeding back into scoring weights via `specs/04-feedback.md`.
