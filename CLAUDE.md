# CongressTrades

Options signal system built on Congressional trade disclosures. Generates actionable plays from STOCK Act filings by tracking politicians on 8 key committees, scoring each trade through a 4-stage pipeline with stock-normalized thresholds, and continuously optimizing via a feedback loop.

## Architecture at a Glance

```
House eFD / Capitol Trades / Finnhub
    ↓
  Agent 1: Data Maintenance (daily, silent)
    ↓
  SQLite DB (data/congress.db)
    ↓
  Agent 2: Daily Signal (weekday mornings)
    Stage 1 → Committee alignment multiplier
    Stage 2 → Opportunity Window Diagnostic (stock-normalized)
    Stage 3 → Forward catalyst search
    Stage 4 → Clustering check (30-day window)
    ↓
  Options module (shared code) → Recommendations
    ↓
  Paper-trading ledger → Feedback loop → Parameter refinement
    ↓
  Email alerts via Resend (daily digest + instant STRONG + weekly deep)
```

## Spec Files — Read Only What You Need

| File | Read when... |
|---|---|
| `specs/01-universe.md` | Building roster generation, committee mapping, backtest agent, politician data model |
| `specs/02-pipeline.md` | Building the signal-scoring pipeline (Stages 1–4), spouse handling, signal tier logic |
| `specs/03-diagnostics.md` | Building Stage 2 specifically — all stock-normalized checks, per-stock data requirements, tunable constants |
| `specs/04-feedback.md` | Building the feedback loop, parameter optimization, weekly review automation, quarterly recalibration |
| `specs/05-options.md` | Building the options module, paper-trading layer, P&L tracking |
| `specs/06-data.md` | Building database schema, data sources, ingestion pipelines, storage |
| `specs/07-agents.md` | Building any of the 5 agents, scheduling, notifications, email delivery |
| `specs/08-deferred.md` | Checking scope boundaries — what is NOT in v1 |

## Tech Stack
- **Language**: Python 3
- **DB**: SQLite → `data/congress.db`
- **Price data**: yfinance (free, historical OHLC, vol) + Massive Market Data (options chains, Greeks, IV)
- **Trade data**: House eFD XML + Capitol Trades + Finnhub API
- **Email**: Resend API via `apesdegen.com`
- **Deployment**: DigitalOcean droplet (scheduling mechanism TBD)

## Build Order
1. **Phase 0**: `specs/06-data.md` → schema + ingestion → `specs/01-universe.md` → backtest agent → generate roster
2. **Phase 1**: `specs/02-pipeline.md` + `specs/03-diagnostics.md` → signal pipeline + `specs/05-options.md` → options module
3. **Phase 2**: `specs/07-agents.md` → wire agents 1-4 + notifications
4. **Phase 3**: `specs/04-feedback.md` → feedback loop integration into Agent 3 + recalibration into Agent 5

## Project Structure
```
~/Desktop/CongressTrades/
├── specs/                          # 8 spec docs (source of truth)
├── config/                         # email distro, finnhub key, runtime config
├── prompts/                        # Agent prompts (1 per agent)
├── scripts/                        # Python modules
│   ├── db.py                       # SQLite helpers
│   ├── db_init.py                  # Schema bootstrap
│   ├── ingest.py                   # Multi-source ingestion
│   ├── pipeline.py                 # Signal pipeline (Stages 1-4)
│   ├── diagnostics.py              # Stage 2 OWD
│   ├── options.py                  # Options module (Greeks, conceptual plays)
│   ├── format_report.py            # Markdown → HTML (existing, reused)
│   └── send_email.py               # Resend delivery (existing, reused)
├── data/
│   ├── congress.db                 # SQLite source of truth
│   └── exports/                    # JSON views for prompts
├── outputs/                        # logs, reports, tmp
├── archive/                        # Deprecated v1 prompts/configs
└── run-agent.sh                    # Subcommand dispatcher
```
