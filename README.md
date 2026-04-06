# Congress Trades Agent

AI agent that monitors U.S. congressional stock trading disclosures and recommends options plays based on the trades of top-performing politicians.

## Quick Start
```bash
./run-agent.sh
```

See [CLAUDE.md](./CLAUDE.md) for full documentation.

## Features
- Tracks 13 politicians across 3 performance tiers (including Pelosi, Tuberville, Crenshaw, Cisneros)
- Pulls data from Finnhub API + web research (Capitol Trades, Quiver Quantitative)
- Breaks down trades by **politician tier** AND by **sector** (11 sectors)
- Identifies sector clustering signals (multi-politician convergence)
- Analyzes committee alignment for information edge assessment
- Recommends options plays (tiered calls/puts with Greeks) for top trades
- Delivers styled HTML email reports via Resend API

## Disclaimer
For research and educational purposes only. Not financial advice. Congressional trading disclosures are delayed up to 45 days under the STOCK Act.
