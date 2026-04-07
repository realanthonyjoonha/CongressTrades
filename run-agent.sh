#!/bin/bash
# CongressTrades Agent Runner — Subcommand Dispatcher
#
# Usage:
#   ./run-agent.sh init-db                       # bootstrap SQLite schema
#   ./run-agent.sh ingest [--year 2025] [--parse-pdfs]  # multi-source ingestion
#   ./run-agent.sh backtest                      # Phase 0 roster generation
#   ./run-agent.sh daily                         # Agent 2 (daily signal)         [Phase 2]
#   ./run-agent.sh weekly                        # Agent 3 (weekly deep)          [Phase 2]
#   ./run-agent.sh deepdive "Politician Name"    # Agent 4 (on-demand)            [Phase 2]
#   ./run-agent.sh tracker                       # Agent 1 (data maintenance)     [Phase 2]
#   ./run-agent.sh db-query "SQL"                # ad-hoc SQL
#
# Phase 0 commands work today. Phase 2 commands print "not yet wired".

set -e
export PATH=$PATH:/usr/local/bin:/opt/homebrew/bin:/Users/anthonyha/Library/Python/3.9/bin

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$BASE_DIR/scripts"
PROMPTS_DIR="$BASE_DIR/prompts"
LOG_DIR="$BASE_DIR/outputs/logs"
REPORT_DIR="$BASE_DIR/outputs/reports"
TMP_DIR="$BASE_DIR/outputs/tmp"
DATA_DIR="$BASE_DIR/data"

mkdir -p "$LOG_DIR" "$REPORT_DIR" "$TMP_DIR" "$DATA_DIR"

TIMESTAMP=$(date '+%Y-%m-%d_%H-%M')
DATE_DISPLAY=$(date '+%B %d, %Y')

cmd="${1:-}"
shift || true

cd "$BASE_DIR"

case "$cmd" in
  init-db)
    echo "[init-db] Bootstrapping SQLite schema..."
    python3 "$SCRIPTS_DIR/db_init.py" "$@"
    ;;

  ingest)
    LOG="$LOG_DIR/ingest_${TIMESTAMP}.log"
    echo "[ingest] $(date) — args: $*" | tee -a "$LOG"
    python3 "$SCRIPTS_DIR/ingest.py" "$@" 2>&1 | tee -a "$LOG"
    ;;

  backtest)
    LOG="$LOG_DIR/backtest_${TIMESTAMP}.log"
    REPORT="$REPORT_DIR/backtest_${TIMESTAMP}.md"
    echo "[backtest] $(date) — generating roster..." | tee -a "$LOG"
    python3 "$SCRIPTS_DIR/backtest.py" --out "$REPORT" "$@" 2>&1 | tee -a "$LOG"
    echo "[backtest] Report: $REPORT"
    ;;

  db-query)
    if [ -z "${1:-}" ]; then
      echo "Usage: $0 db-query \"SELECT ...\""
      exit 1
    fi
    sqlite3 "$DATA_DIR/congress.db" "$@"
    ;;

  db)
    python3 "$SCRIPTS_DIR/db.py" "$@"
    ;;

  daily|weekly|deepdive|tracker)
    echo "[$cmd] not yet wired — Phase 2 work. See specs/07-agents.md"
    exit 2
    ;;

  ""|help|-h|--help)
    echo "CongressTrades Agent Runner"
    echo ""
    echo "Phase 0 (active):"
    echo "  init-db                       Bootstrap SQLite schema"
    echo "  ingest --source house-efd --year 2025 --parse-pdfs"
    echo "  ingest --source finnhub --symbol NVDA"
    echo "  backtest [--lookback 5] [--report-only]"
    echo "  db params|politicians|trades|mappings  Ad-hoc DB queries"
    echo "  db-query \"SELECT ...\"         Raw SQL"
    echo ""
    echo "Phase 2 (planned):"
    echo "  daily | weekly | deepdive | tracker"
    ;;

  *)
    echo "Unknown command: $cmd"
    echo "Run: $0 help"
    exit 1
    ;;
esac
