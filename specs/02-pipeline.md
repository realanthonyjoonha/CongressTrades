# 02 — Signal-Scoring Pipeline

Every fresh disclosure from a roster member runs through Stages 1–4 in order. A trade must survive each stage to be recommended.

## Pre-Pipeline: Trade Tagging

Every disclosure tagged as: `member_direct` | `spouse` | `dependent`
- `dependent` trades → logged, no score, no recommendation
- `spouse` trades → run through pipeline with adjustments (see Spouse Rules below)
- `member_direct` → standard pipeline

## Stage 1 — Committee Alignment Multiplier

No hard filter. Assigns a multiplier that carries forward to final tier:

| Condition | Multiplier |
|---|---|
| Ticker sector maps to trader's committee | 1.0x |
| Mega-cap override list + trader on a mapped committee | 1.0x |
| Mega-cap override list + trader NOT on a mapped committee | 0.5x ("tangential") |
| Chamber-wide leadership (Speaker, floor leader, whip, caucus chair) | 0.7x floor on all trades |
| Committee-specific leadership (chair, ranking member) | 0.7x floor within own committee only |
| Off-committee, non-leadership, non-override | 0.3x (needs strong clustering to reach BASE) |

See `specs/01-universe.md` for committee list and override tickers.

## Stage 2 — Opportunity Window Diagnostic

Full spec in `specs/03-diagnostics.md`. Summary: evaluates Entry Quality (has stock moved too far?) and Catalyst Status (has insider edge been consumed?) using stock-normalized thresholds. Produces a point score:

- **0–1 points** → Window open, proceed
- **2–3 points** → Window narrowing, proceed with "late entry" flag
- **4+ points** → Window closed, log as `opportunity-expired`, stop

## Stage 3 — Forward Catalyst Identification

For trades surviving Stage 2, agent searches for:
- Upcoming committee hearings or markups in the politician's committee
- Federal contract / budget cycle events: DoD appropriations, FDA PDUFA dates, FERC rulings, CR deadlines, earnings within holding window

| Result | Signal level |
|---|---|
| Forward catalyst identified in next 90 days | BASE |
| No forward catalyst identified | MODERATE (still actionable if clustering confirms) |

Agent performs 5–10 web searches per trade for catalyst research.

## Stage 4 — Clustering Check

Query DB: other roster members holding or buying same ticker / same sub-sector within past `k_cluster_window` days (default: **30 days**).

30-day window matches STOCK Act disclosure lag reality — politicians who traded around the same time file at different points in the 45-day window.

**Bump signal up one tier if:**
- 3+ politicians clustering, OR
- Cross-party clustering (≥2 from each party)

## Final Signal Tiers

| Tier | Criteria | Options treatment |
|---|---|---|
| **STRONG** | 1.0x aligned + forward catalyst + clustering, window open | Real Greeks via MMD, specific strikes |
| **BASE** | 1.0x aligned + forward catalyst, window open | Conceptual play (DTE window, delta target) |
| **MODERATE** | Aligned only, some ambiguity | Conceptual play, lighter weight |
| **SKIP** | Failed pipeline | Logged, no recommendation |

Multiplier affects tier eligibility: 0.5x can reach BASE max. 0.3x needs strong clustering for BASE.

## Spouse Trade Rules

1. **Alignment**: Inherits member's committee assignments
2. **OWD penalty**: If 3+ major outlets covered the trade (detected via web search for `[member] + [ticker] + trade` within 48hr of disclosure), +1 point on Entry Quality dimension
3. **Clustering weight**: 0.5x — two spouse trades = one member-direct trade in cluster count
4. **Tier cap**: Watchlist-tier member's spouse → BASE max. Core-tier member's spouse → can reach STRONG if window wide open (0–1 OWD points) AND clustering with member-direct trades confirms
5. **P&L tracking**: Separate `spouse` tag in paper-trading ledger, reported separately in edge database
