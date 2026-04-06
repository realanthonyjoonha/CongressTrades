#!/bin/bash
# Congress Trades Agent Runner
# Two-pass: Claude does research + writes to file, Python converts to HTML, Resend emails

export PATH=$PATH:/usr/local/bin:/opt/homebrew/bin

BASE_DIR=/Users/anthonyha/Desktop/CongressTrades
LOG_DIR=$BASE_DIR/outputs/logs
REPORT_DIR=$BASE_DIR/outputs/reports
TMP_DIR=$BASE_DIR/outputs/tmp
mkdir -p $LOG_DIR $REPORT_DIR $TMP_DIR

TIMESTAMP=$(date '+%Y-%m-%d_%H-%M')
DATE_DISPLAY=$(date '+%B %d, %Y')
LOG_FILE="$LOG_DIR/congress-trades_${TIMESTAMP}.log"
REPORT_FILE="$REPORT_DIR/congress-trades_${TIMESTAMP}.html"
RESEARCH_FILE="$TMP_DIR/congress-trades_${TIMESTAMP}_research.md"

echo "[$(date)] Starting Congress Trades Agent" >> $LOG_FILE

# Pull latest from GitHub
cd $BASE_DIR && git pull >> $LOG_FILE 2>&1

########################################
# CALL 1: Research — Claude does all tool calls and writes findings to file
########################################
cat > "$TMP_DIR/prompt_${TIMESTAMP}.txt" << PROMPTEOF
Read prompts/congress-trades.md and execute it fully. Do all web searches, all API calls, all analysis.

CRITICAL: When you are done with ALL your research, you MUST write the complete, detailed findings to a file using the Write tool. Write to this exact path:
$RESEARCH_FILE

The file must contain your FULL detailed report — every politician's trades analyzed, sector breakdowns, options plays with Greeks, committee alignment analysis.

Write in markdown format. Target 5000+ words. Do NOT summarize — write the FULL analysis. This file is the deliverable.

Do NOT use Gmail or email tools. Just do the research and write the findings file.
PROMPTEOF

echo "[$(date)] CALL 1: Starting research phase..." >> $LOG_FILE

cd $BASE_DIR
cat "$TMP_DIR/prompt_${TIMESTAMP}.txt" | claude --print --dangerously-skip-permissions > /dev/null 2>> $LOG_FILE

# Check if research file was created
if [ -f "$RESEARCH_FILE" ]; then
    RESEARCH_SIZE=$(wc -c < "$RESEARCH_FILE")
    echo "[$(date)] CALL 1 complete: Research file created (${RESEARCH_SIZE} bytes)" >> $LOG_FILE
else
    echo "[$(date)] CALL 1 FAILED: No research file created. Falling back to raw output." >> $LOG_FILE
    cat "$TMP_DIR/prompt_${TIMESTAMP}.txt" | claude --print --dangerously-skip-permissions > "$RESEARCH_FILE" 2>> $LOG_FILE
    RESEARCH_SIZE=$(wc -c < "$RESEARCH_FILE")
    echo "[$(date)] Fallback captured: ${RESEARCH_SIZE} bytes" >> $LOG_FILE
fi

########################################
# Convert research to styled HTML
########################################
echo "[$(date)] Converting to HTML..." >> $LOG_FILE

# Extract subject from research
SUBJECT=$(grep "^SUBJECT:" "$RESEARCH_FILE" 2>/dev/null | tail -1 | sed 's/^SUBJECT: *//')
if [ -z "$SUBJECT" ]; then
    SUBJECT="Congress Trades Report — $DATE_DISPLAY"
fi

# Remove SUBJECT line and convert to HTML
grep -v "^SUBJECT:" "$RESEARCH_FILE" | python3 $BASE_DIR/scripts/format_report.py > "$REPORT_FILE"

REPORT_SIZE=$(wc -c < "$REPORT_FILE")
echo "[$(date)] Final HTML report: ${REPORT_SIZE} bytes" >> $LOG_FILE

if [ "$REPORT_SIZE" -lt 3000 ]; then
    SUBJECT="[PARTIAL] $SUBJECT"
fi

# Send email via Resend
python3 $BASE_DIR/scripts/send_email.py "$SUBJECT" "$REPORT_FILE" 2>> $LOG_FILE

echo "[$(date)] Finished Congress Trades Agent — emailed (${REPORT_SIZE} bytes)" >> $LOG_FILE

# Clean up old tmp files (keep last 7 days)
find $TMP_DIR -name "*.txt" -mtime +7 -delete 2>/dev/null
find $TMP_DIR -name "*.md" -mtime +7 -delete 2>/dev/null
