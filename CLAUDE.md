# Congress Trades Agent

## Project Overview
Standalone AI agent that monitors U.S. congressional stock trading disclosures, identifies the most notable trades from top-performing politicians, and recommends options plays to follow the "smart money" signal.

This is **independent** of the energy/AI trading project — it covers all sectors and runs on-demand.

## What It Does
1. Pulls recent congressional trade disclosures via Finnhub API + web research (Capitol Trades, Quiver Quantitative)
2. Cross-references trades against 13 tracked politicians (3 tiers based on historical performance)
3. Categorizes trades by **politician tier** AND by **sector**
4. Identifies sector clustering and committee alignment signals
5. Recommends options plays (tiered calls/puts with Greeks) for top 5-8 trades
6. Emails the report to all 9 recipients via Resend API

## Tracked Politicians (13 total)

### Tier 1 — Top Performers (5)
- Nancy Pelosi (D-CA)
- Dan Crenshaw (R-TX)
- Tommy Tuberville (R-AL)
- Mark Green (R-TN)
- Josh Gottheimer (D-NJ)

### Tier 2 — Active Traders (5)
- Gil Cisneros (D-CA)
- Marjorie Taylor Greene (R-GA)
- Michael McCaul (R-TX)
- Ro Khanna (D-CA)
- Dave Joyce (R-OH)

### Tier 3 — Watchlist (3)
- Austin Scott (R-GA)
- Pat Fallon (R-TX)
- Shelley Moore Capito (R-WV)

## Sectors Tracked
- Technology & AI
- Semiconductors
- Defense & Aerospace
- Energy & Utilities
- Healthcare & Pharma
- Finance & Banking
- Industrials & Infrastructure
- Cybersecurity
- Real Estate
- Consumer & Retail
- Other

## How to Run
```bash
cd ~/Desktop/CongressTrades
./run-agent.sh
```

The agent will:
1. Pull congressional trade data (Finnhub API + web)
2. Analyze and categorize all trades
3. Generate options play recommendations
4. Write report to `outputs/reports/`
5. Email to all 9 recipients

## Project Structure
```
CongressTrades/
├── config/
│   ├── congress-trades.json    # Politicians, API key, sectors, filters
│   └── email-distro.json      # Email recipients (9 people)
├── prompts/
│   └── congress-trades.md      # Full agent prompt template
├── scripts/
│   ├── format_report.py        # Markdown → HTML email (dark theme)
│   └── send_email.py           # Resend API email delivery
├── outputs/
│   ├── logs/                   # Execution logs
│   ├── reports/                # Final HTML reports
│   └── tmp/                    # Raw research markdown
├── run-agent.sh                # Main runner script
└── CLAUDE.md                   # This file
```

## Data Sources
- **Finnhub API** (free tier, 60 calls/min) — congressional trading endpoint
- **Web Search** — Capitol Trades, Quiver Quantitative, news outlets
- **Massive Market Data API** — stock prices, options chains, Greeks (uses parent project's MMD MCP)
- **Resend API** — email delivery via verified `apesdegen.com` domain

## Output Sections
1. Executive Summary
2. Congressional Trading Dashboard (all tracked politicians)
3. **Trades by Politician Tier** (Tier 1 → Tier 2 → Tier 3)
4. **Trades by Sector** (every trade categorized into 11 sectors)
5. Sector Clustering Analysis (multi-politician convergence signals)
6. Notable Trades — Deep Analysis with Options Plays (top 5-8)
7. Committee Intelligence
8. Performance Tracker
9. Trades NOT Worth Following

## Key Conventions
- **STOCK Act disclosure lag**: Trades can be reported up to 45 days after execution. Reports always show transaction date vs disclosure date and flag stale signals.
- **Edge staleness**: If a stock has moved 15%+ since the trade, the report flags "edge likely priced in."
- **Position sizing**: 1-3% per trade (signals, not deep convictions).
- **Committee alignment**: Strongest signals come from politicians on relevant committees (Armed Services for defense, Energy & Commerce for energy, etc.).
- **Sector clustering**: 3+ politicians buying the same sector = STRONG SIGNAL, especially bipartisan.

## Email Delivery
- **Provider**: Resend API
- **Sender**: APES Research <research@apesdegen.com>
- **Domain**: apesdegen.com (DKIM, SPF, DMARC verified)
- **Recipients**: 9 (same as parent Trading project)

## Disclaimer
This agent is for research and educational purposes only. Congressional trading disclosures are delayed (up to 45 days), so signals are not real-time. This is NOT financial advice. Always do your own due diligence.
