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

  deepdive)
    POLITICIAN="${1:-}"
    if [ -z "$POLITICIAN" ]; then
      echo "Usage: $0 deepdive \"Politician Name\""
      echo "Example: $0 deepdive \"Mark Green\""
      exit 1
    fi
    shift

    # Slug for filenames: "Mark Green" -> "mark_green"
    SLUG=$(echo "$POLITICIAN" | tr '[:upper:] ' '[:lower:]_' | tr -cd '[:alnum:]_')

    LOG="$LOG_DIR/deepdive_${SLUG}_${TIMESTAMP}.log"
    RESEARCH_PACK="$TMP_DIR/deepdive_${SLUG}_${TIMESTAMP}_pack.md"
    NARRATIVE="$TMP_DIR/deepdive_${SLUG}_${TIMESTAMP}_narrative.md"
    REPORT="$REPORT_DIR/deepdive_${SLUG}_${TIMESTAMP}.html"
    DEEPDIVE_RECIPIENT="${DEEPDIVE_RECIPIENT:-anthonyjoonha@gmail.com}"

    echo "[deepdive] $(date) — politician: $POLITICIAN" | tee -a "$LOG"
    echo "[deepdive] slug: $SLUG" | tee -a "$LOG"
    echo "[deepdive] recipient: $DEEPDIVE_RECIPIENT" | tee -a "$LOG"

    # ---- Phase A: Python analytical driver ----
    echo "[deepdive] Phase A — running analytical driver..." | tee -a "$LOG"
    set +e
    python3 "$SCRIPTS_DIR/deepdive.py" "$POLITICIAN" \
        --out "$RESEARCH_PACK" "$@" 2>&1 | tee -a "$LOG"
    PY_EXIT=${PIPESTATUS[0]}
    set -e
    if [ "$PY_EXIT" -ne 0 ]; then
      echo "[deepdive] ERROR: Phase A driver exited $PY_EXIT" | tee -a "$LOG"
      exit 1
    fi

    if [ ! -s "$RESEARCH_PACK" ]; then
      echo "[deepdive] ERROR: research pack empty or missing at $RESEARCH_PACK" | tee -a "$LOG"
      exit 1
    fi
    PACK_BYTES=$(wc -c < "$RESEARCH_PACK")
    echo "[deepdive] Phase A complete — research pack: ${PACK_BYTES} bytes" | tee -a "$LOG"

    # ---- Phase B: LLM narrative layer ----
    echo "[deepdive] Phase B — invoking Claude for narrative synthesis..." | tee -a "$LOG"

    # Substitute template variables in the prompt
    PROMPT=$(sed \
        -e "s|{POLITICIAN_NAME}|$POLITICIAN|g" \
        -e "s|{RESEARCH_PACK_PATH}|$RESEARCH_PACK|g" \
        "$PROMPTS_DIR/deepdive.md")

    # Run Claude with stdout capture as the deliverable.
    # Background + wait pattern lets us add a hard timeout if needed.
    echo "$PROMPT" | claude --print --dangerously-skip-permissions \
        > "$NARRATIVE" 2>> "$LOG" || {
      echo "[deepdive] WARN: claude exited non-zero, checking output..." | tee -a "$LOG"
    }

    if [ ! -s "$NARRATIVE" ]; then
      echo "[deepdive] ERROR: narrative file empty — Claude subprocess produced no output" | tee -a "$LOG"
      exit 1
    fi
    NARRATIVE_BYTES=$(wc -c < "$NARRATIVE")
    echo "[deepdive] Phase B complete — narrative: ${NARRATIVE_BYTES} bytes" | tee -a "$LOG"

    # Check for auth failure markers
    if grep -qiE "invalid api key|please run /login|authentication failed" "$NARRATIVE"; then
      echo "[deepdive] ERROR: Claude auth failure detected — run 'claude login'" | tee -a "$LOG"
      exit 1
    fi

    # ---- Phase C: HTML + email delivery (user only) ----
    echo "[deepdive] Phase C — formatting HTML and sending email..." | tee -a "$LOG"

    # Extract SUBJECT line; fall back to a sensible default
    SUBJECT=$(grep "^SUBJECT:" "$NARRATIVE" | head -1 | sed 's/^SUBJECT: *//')
    if [ -z "$SUBJECT" ]; then
      SUBJECT="Deep-Dive — $POLITICIAN ($DATE_DISPLAY)"
      echo "[deepdive] WARN: no SUBJECT line found, using default: $SUBJECT" | tee -a "$LOG"
    fi
    echo "[deepdive] subject: $SUBJECT" | tee -a "$LOG"

    # Strip the SUBJECT line before formatting (it shouldn't appear in the email body)
    grep -v "^SUBJECT:" "$NARRATIVE" | python3 "$SCRIPTS_DIR/format_report.py" > "$REPORT"

    if [ ! -s "$REPORT" ]; then
      echo "[deepdive] ERROR: HTML report empty after format_report.py" | tee -a "$LOG"
      exit 1
    fi

    python3 "$SCRIPTS_DIR/send_email.py" \
        --subject "$SUBJECT" \
        --html-file "$REPORT" \
        --to "$DEEPDIVE_RECIPIENT" 2>&1 | tee -a "$LOG"

    echo "[deepdive] DONE — report: $REPORT" | tee -a "$LOG"
    ;;

  tracker)
    LOG="$LOG_DIR/tracker_${TIMESTAMP}.log"
    echo "[tracker] $(date) — starting data maintenance run" | tee -a "$LOG"

    set +e
    python3 "$SCRIPTS_DIR/data_maintenance.py" "$@" 2>&1 | tee -a "$LOG"
    EXIT_CODE=${PIPESTATUS[0]}
    set -e

    if [ "$EXIT_CODE" -eq 0 ]; then
      echo "[tracker] DONE — $(date)" | tee -a "$LOG"
    else
      echo "[tracker] FAILED with exit $EXIT_CODE — $(date)" | tee -a "$LOG"
      exit "$EXIT_CODE"
    fi
    ;;

  daily|weekly)
    echo "[$cmd] not yet wired — Phase 2.3+ work. See specs/07-agents.md"
    exit 2
    ;;

  ""|help|-h|--help)
    echo "CongressTrades Agent Runner"
    echo ""
    echo "Phase 0 + 2.1 + 2.2 (active):"
    echo "  init-db                              Bootstrap SQLite schema"
    echo "  ingest --source house-efd --year 2025 --parse-pdfs"
    echo "  ingest --source finnhub --symbol NVDA"
    echo "  backtest [--lookback 5] [--report-only]"
    echo "  deepdive \"Politician Name\"           Agent 4 - single-politician report"
    echo "  tracker [--dry-run] [--since DATE]   Agent 2 - daily data maintenance"
    echo "  db params|politicians|trades|mappings|latest-disclosure  Ad-hoc DB queries"
    echo "  db-query \"SELECT ...\"               Raw SQL"
    echo ""
    echo "Phase 2.3+ (planned):"
    echo "  daily | weekly"
    echo ""
    echo "Env vars:"
    echo "  DEEPDIVE_RECIPIENT=foo@bar.com       Override Deep-Dive email (default: admin)"
    ;;

  *)
    echo "Unknown command: $cmd"
    echo "Run: $0 help"
    exit 1
    ;;
esac
