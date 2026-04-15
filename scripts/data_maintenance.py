#!/usr/bin/env python3
"""
data_maintenance.py — Agent 2 (Data Maintenance) wrapper for the
CongressTrades daily ingestion pipeline.

Phase 2.2 of the build plan. This is a thin Python wrapper around
scripts/ingest.py that:
  1. Computes a dynamic --since date by querying the DB for the most recent
     disclosure date and backing off a 7-day safety buffer (catches
     retroactive filings and missed cron runs).
  2. Falls back to today - 30 days if the DB is empty.
  3. Invokes ingest.py as a subprocess with the right --source / --year /
     --since / --parse-pdfs flags for an incremental daily pull.
  4. Captures stdout, stderr, and exit code.
  5. Parses the "[ingest] persisted: N trades, M placeholders skipped" line
     to extract success stats.
  6. Detects ingestion failures (non-zero exit, "FAILED" markers in output,
     or zero-persisted-trades-with-error-pattern).
  7. On success: silent (matches the spec — "no email unless error").
  8. On failure: builds an HTML error report and dispatches it via
     scripts/send_email.py with --to anthonyjoonha@gmail.com (admin only).

CLI:
    python3 scripts/data_maintenance.py                       # full auto
    python3 scripts/data_maintenance.py --dry-run             # no DB writes
    python3 scripts/data_maintenance.py --since 2026-03-01    # manual override
    python3 scripts/data_maintenance.py --admin foo@bar.com   # alert recipient
    python3 scripts/data_maintenance.py --no-alert            # silent failures
    python3 scripts/data_maintenance.py --source finnhub      # source override

Exit codes:
    0 — success (or dry-run completed cleanly)
    1 — ingestion failed (admin alerted unless --no-alert)
    2 — internal wrapper error (admin alerted unless --no-alert)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

# Local imports — same directory as ingest.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"
LOG_DIR = BASE_DIR / "outputs" / "logs"
TMP_DIR = BASE_DIR / "outputs" / "tmp"

DEFAULT_BUFFER_DAYS = 7
DEFAULT_FALLBACK_DAYS = 30
DEFAULT_ADMIN_EMAIL = "anthonyjoonha@gmail.com"
DEFAULT_INGEST_TIMEOUT_SEC = 3600  # 1 hour hard timeout
DEFAULT_MAX_PDFS = 200

# Regex to extract the ingest stats line
PERSISTED_RE = re.compile(
    r"\[ingest\]\s+persisted:\s+(\d+)\s+trades,\s+(\d+)\s+placeholders\s+skipped"
)
FAILED_RE = re.compile(r"\[(house-efd|finnhub|ingest)\]\s+FAILED", re.IGNORECASE)


# ---------------------------------------------------------------------------
# --since computation
# ---------------------------------------------------------------------------

def compute_since_date(
    conn,
    buffer_days: int = DEFAULT_BUFFER_DAYS,
    fallback_days: int = DEFAULT_FALLBACK_DAYS,
    source: Optional[str] = None,
) -> str:
    """
    Compute the --since date for the next ingestion run.

    Strategy: max(disclosure_date) - buffer_days, with fallback to
    today - fallback_days if the DB has no rows. The buffer protects
    against retroactive House eFD filings and missed cron runs.

    Returns ISO date string "YYYY-MM-DD".
    """
    latest = db.get_latest_disclosure_date(conn, source=source)
    if not latest:
        fallback = datetime.utcnow().date() - timedelta(days=fallback_days)
        return fallback.strftime("%Y-%m-%d")

    try:
        latest_dt = datetime.strptime(latest, "%Y-%m-%d").date()
    except ValueError:
        # Malformed date in DB — fall back to safe default
        fallback = datetime.utcnow().date() - timedelta(days=fallback_days)
        return fallback.strftime("%Y-%m-%d")

    since_dt = latest_dt - timedelta(days=buffer_days)
    return since_dt.strftime("%Y-%m-%d")


def compute_years_to_pull(today: Optional[datetime] = None) -> List[int]:
    """
    Decide which year(s) to pass to ingest.py.

    Default: current year.
    January 1–15 rollover: also include the prior year, since late-December
    House eFD filings can still trickle in during the first half of January.
    """
    if today is None:
        today = datetime.utcnow()
    years = [today.year]
    if today.month == 1 and today.day <= 15:
        years.append(today.year - 1)
    return years


# ---------------------------------------------------------------------------
# Subprocess invocation
# ---------------------------------------------------------------------------

def build_ingest_args(
    since: str,
    year: int,
    source: str = "house-efd",
    parse_pdfs: bool = True,
    max_pdfs: int = DEFAULT_MAX_PDFS,
    dry_run: bool = False,
) -> List[str]:
    """Build the argv list for the ingest.py subprocess."""
    args = [
        sys.executable,
        str(SCRIPTS_DIR / "ingest.py"),
        "--source", source,
        "--year", str(year),
        "--since", since,
    ]
    if parse_pdfs and source == "house-efd":
        args.extend(["--parse-pdfs", "--max-pdfs", str(max_pdfs)])
    if dry_run:
        args.append("--dry-run")
    return args


def run_ingest(
    args: List[str],
    timeout_sec: int = DEFAULT_INGEST_TIMEOUT_SEC,
) -> Tuple[int, str, str]:
    """
    Run ingest.py as a subprocess. Returns (exit_code, stdout, stderr).

    Streams progress to stderr in real time so the cron log captures it,
    while also accumulating both streams for parsing afterward.
    """
    print(f"[tracker] running: {' '.join(args)}", file=sys.stderr)
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired as e:
        partial_stdout = (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        partial_stderr = (e.stderr or b"").decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return (
            -1,
            partial_stdout,
            partial_stderr + f"\n[tracker] TIMEOUT after {timeout_sec}s",
        )


def parse_ingest_stats(stdout: str) -> dict:
    """
    Parse the "[ingest] persisted: N trades, M placeholders skipped" line.
    Returns {"trades_persisted": int, "placeholders_skipped": int}.
    Returns zeros if the line is missing.
    """
    match = PERSISTED_RE.search(stdout)
    if not match:
        return {"trades_persisted": 0, "placeholders_skipped": 0, "parsed": False}
    return {
        "trades_persisted": int(match.group(1)),
        "placeholders_skipped": int(match.group(2)),
        "parsed": True,
    }


def detect_ingestion_failure(
    exit_code: int,
    stdout: str,
    stderr: str,
    stats: dict,
) -> Optional[str]:
    """
    Heuristic failure detector. Returns a failure reason string, or None
    if the run looks healthy.

    Failure modes detected:
      1. Subprocess exited non-zero
      2. ingest.py printed a [source] FAILED marker (silent failure mode)
      3. Subprocess timeout (exit code -1)
    """
    if exit_code == -1:
        return "ingest subprocess timed out"
    if exit_code != 0:
        return f"ingest subprocess exited with code {exit_code}"
    if FAILED_RE.search(stdout) or FAILED_RE.search(stderr):
        match = FAILED_RE.search(stdout) or FAILED_RE.search(stderr)
        return f"ingest reported a source failure: {match.group(0) if match else 'unknown'}"
    if not stats.get("parsed"):
        return "ingest stats line missing — script may not have completed"
    return None


# ---------------------------------------------------------------------------
# Error reporting
# ---------------------------------------------------------------------------

def format_error_report(
    reason: str,
    exit_code: int,
    args: List[str],
    stdout: str,
    stderr: str,
    since: str,
    year: int,
    timestamp: str,
) -> str:
    """Build a small HTML error report for the admin alert email."""
    def _tail(text: str, lines: int = 50) -> str:
        if not text:
            return "(empty)"
        text_lines = text.splitlines()
        return "\n".join(text_lines[-lines:])

    cmd_str = " ".join(args)
    stdout_tail = _tail(stdout, 50)
    stderr_tail = _tail(stderr, 50)

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:0;">
<div style="max-width:900px;margin:0 auto;padding:24px;">

<div style="background:#1a1f2e;border:1px solid #f85149;border-radius:12px;padding:24px;margin-bottom:24px;">
<div style="font-size:22px;font-weight:700;color:#f85149;">[ERROR] Data Maintenance Failed</div>
<div style="font-size:13px;color:#8b949e;margin-top:6px;">CongressTrades · Agent 2 · {timestamp}</div>
</div>

<div style="background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin-bottom:16px;">
<div style="font-weight:600;color:#58a6ff;margin-bottom:8px;">Reason</div>
<div style="font-family:monospace;font-size:13px;">{reason}</div>
</div>

<div style="background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin-bottom:16px;">
<div style="font-weight:600;color:#58a6ff;margin-bottom:8px;">Run details</div>
<table style="font-size:12px;border-collapse:collapse;">
<tr><td style="padding:4px 12px 4px 0;color:#8b949e;">Exit code</td><td style="font-family:monospace;">{exit_code}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#8b949e;">--since</td><td style="font-family:monospace;">{since}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#8b949e;">--year</td><td style="font-family:monospace;">{year}</td></tr>
<tr><td style="padding:4px 12px 4px 0;color:#8b949e;">Command</td><td style="font-family:monospace;font-size:11px;word-break:break-all;">{cmd_str}</td></tr>
</table>
</div>

<div style="background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin-bottom:16px;">
<div style="font-weight:600;color:#58a6ff;margin-bottom:8px;">stderr (last 50 lines)</div>
<pre style="font-size:11px;color:#e6edf3;background:#0d1117;padding:12px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;">{stderr_tail}</pre>
</div>

<div style="background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px;margin-bottom:16px;">
<div style="font-weight:600;color:#58a6ff;margin-bottom:8px;">stdout (last 50 lines)</div>
<pre style="font-size:11px;color:#e6edf3;background:#0d1117;padding:12px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;">{stdout_tail}</pre>
</div>

<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:16px;text-align:center;">
<p style="font-size:11px;color:#6e7681;margin:0;">CongressTrades Data Maintenance Agent · Run via run-agent.sh tracker</p>
</div>

</div>
</body>
</html>
"""
    return html


def send_admin_alert(
    subject: str,
    html: str,
    recipient: str,
) -> bool:
    """
    Dispatch an admin alert via send_email.py --to.

    Writes the HTML to a temp file (cleaner than passing on the CLI which
    has length limits), then shells out to send_email.py.
    """
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    html_path = TMP_DIR / f"tracker_alert_{timestamp}.html"
    html_path.write_text(html)

    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "send_email.py"),
        "--subject", subject,
        "--html-file", str(html_path),
        "--to", recipient,
    ]
    print(f"[tracker] sending admin alert to {recipient}", file=sys.stderr)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            print("[tracker] alert sent successfully", file=sys.stderr)
            return True
        print(f"[tracker] send_email.py exited {result.returncode}", file=sys.stderr)
        print(f"[tracker] stderr: {result.stderr}", file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("[tracker] send_email.py timed out after 120s", file=sys.stderr)
        return False
    except Exception as e:
        print(f"[tracker] send_email.py invocation error: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Agent 2 — Data Maintenance wrapper around ingest.py",
    )
    ap.add_argument("--since", help="Manual override for --since (skips dynamic lookup)")
    ap.add_argument("--source", default="house-efd",
                    choices=["house-efd", "finnhub"],
                    help="Source to ingest from (default: house-efd)")
    ap.add_argument("--max-pdfs", type=int, default=DEFAULT_MAX_PDFS,
                    help=f"PDF parse cap (default {DEFAULT_MAX_PDFS})")
    ap.add_argument("--no-parse-pdfs", action="store_true",
                    help="Skip PTR PDF parsing (placeholder rows only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Don't write to DB; admin alert still sent on failure")
    ap.add_argument("--admin", default=DEFAULT_ADMIN_EMAIL,
                    help=f"Admin email for failure alerts (default {DEFAULT_ADMIN_EMAIL})")
    ap.add_argument("--no-alert", action="store_true",
                    help="Suppress failure alerts (for testing)")
    ap.add_argument("--buffer-days", type=int, default=DEFAULT_BUFFER_DAYS,
                    help=f"Days to subtract from latest disclosure (default {DEFAULT_BUFFER_DAYS})")
    ap.add_argument("--timeout", type=int, default=DEFAULT_INGEST_TIMEOUT_SEC,
                    help=f"Subprocess timeout in seconds (default {DEFAULT_INGEST_TIMEOUT_SEC})")
    ap.add_argument("--sentinel-path", default=None,
                    help="Path to write a JSON sentinel file after the ingest "
                         "finishes. run-agent.sh reads this to decide whether "
                         "to run the Sonnet Phase B (if new_trade_ids is "
                         "non-empty).")
    args = ap.parse_args()

    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[tracker] start — {timestamp}", file=sys.stderr)
    print(f"[tracker] dry_run={args.dry_run}, alert={'off' if args.no_alert else args.admin}",
          file=sys.stderr)

    # ---- Compute --since ----
    try:
        conn = db.connect()
        if args.since:
            since = args.since
            print(f"[tracker] --since (manual override): {since}", file=sys.stderr)
        else:
            since = compute_since_date(conn, buffer_days=args.buffer_days)
            latest = db.get_latest_disclosure_date(conn)
            print(f"[tracker] latest disclosure in DB: {latest or '(none)'}",
                  file=sys.stderr)
            print(f"[tracker] --since (computed): {since}  "
                  f"(buffer={args.buffer_days}d)", file=sys.stderr)
        conn.close()
    except Exception as e:
        print(f"[tracker] FATAL: failed to compute --since: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 2

    # ---- Determine which year(s) to pull ----
    years = compute_years_to_pull()
    print(f"[tracker] years to pull: {years}", file=sys.stderr)

    # Snapshot the max trade_id BEFORE the run so we can identify new
    # rows after. The daily agent consumes this via tracker_runs.new_trade_ids.
    pre_max_trade_id = 0
    try:
        conn_pre = db.connect()
        cur_pre = conn_pre.cursor()
        cur_pre.execute("SELECT COALESCE(MAX(id), 0) FROM trades")
        pre_max_trade_id = cur_pre.fetchone()[0] or 0
        conn_pre.close()
    except Exception as e:
        print(f"[tracker] WARN: failed to snapshot max trade_id: {e}", file=sys.stderr)

    # ---- Run ingest for each year ----
    run_start = datetime.utcnow().isoformat()
    overall_failure = None
    total_persisted = 0
    total_skipped = 0
    all_stdout: List[str] = []
    all_stderr: List[str] = []
    last_args: List[str] = []

    for year in years:
        ingest_args = build_ingest_args(
            since=since,
            year=year,
            source=args.source,
            parse_pdfs=not args.no_parse_pdfs,
            max_pdfs=args.max_pdfs,
            dry_run=args.dry_run,
        )
        last_args = ingest_args

        exit_code, stdout, stderr = run_ingest(ingest_args, timeout_sec=args.timeout)
        all_stdout.append(stdout)
        all_stderr.append(stderr)

        # Echo subprocess output to our stderr so the runner log captures it
        if stdout:
            sys.stderr.write(stdout)
        if stderr:
            sys.stderr.write(stderr)
        sys.stderr.flush()

        stats = parse_ingest_stats(stdout)
        total_persisted += stats.get("trades_persisted", 0)
        total_skipped += stats.get("placeholders_skipped", 0)

        failure_reason = detect_ingestion_failure(exit_code, stdout, stderr, stats)
        if failure_reason:
            overall_failure = (failure_reason, exit_code, year)
            print(f"[tracker] FAILURE detected for year={year}: {failure_reason}",
                  file=sys.stderr)
            break  # Don't continue to other years on failure

    # ---- Compute new trade IDs + distinct politician names ----
    # Runs AFTER the ingest loop (whether success or failure) so we can
    # record what landed in this run. Failed runs with 0 new trades
    # still get a tracker_runs row for the audit trail.
    new_trade_ids: List[int] = []
    new_politician_names: List[str] = []
    if not args.dry_run:
        try:
            conn_post = db.connect()
            cur_post = conn_post.cursor()
            cur_post.execute(
                "SELECT id, politician_name FROM trades WHERE id > ? ORDER BY id ASC",
                (pre_max_trade_id,),
            )
            rows = cur_post.fetchall()
            new_trade_ids = [r["id"] for r in rows]
            seen = set()
            for r in rows:
                name = r["politician_name"]
                if name and name not in seen:
                    seen.add(name)
                    new_politician_names.append(name)
            conn_post.close()
        except Exception as e:
            print(f"[tracker] WARN: failed to diff new trade ids: {e}",
                  file=sys.stderr)

    # ---- Write new_trade_ids sentinel file for the runner ----
    # run-agent.sh reads this to decide whether to invoke the Sonnet
    # Phase B (only runs if the sentinel file has ≥1 trade id).
    if args.sentinel_path:
        try:
            from pathlib import Path as _Path
            p = _Path(args.sentinel_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({
                "run_timestamp": run_start,
                "new_trade_ids": new_trade_ids,
                "new_politician_names": new_politician_names,
                "trades_persisted": total_persisted,
                "since_date": since,
                "years": years,
            }))
            print(f"[tracker] wrote sentinel: {args.sentinel_path}"
                  f" (new_trades={len(new_trade_ids)})", file=sys.stderr)
        except Exception as e:
            print(f"[tracker] WARN: failed to write sentinel: {e}",
                  file=sys.stderr)

    # ---- Persist tracker_runs row ----
    if not args.dry_run:
        try:
            conn_log = db.connect()
            db.insert_tracker_run(conn_log, {
                "run_timestamp": run_start,
                "completed_at": datetime.utcnow().isoformat(),
                "source": args.source,
                "year": years[0] if years else None,
                "since_date": since,
                "trades_persisted": total_persisted,
                "placeholders_skipped": total_skipped,
                "new_trade_ids": new_trade_ids,
                "new_politician_names": new_politician_names,
                "email_sent": False,       # set to True later by Phase B wrapper
                "email_subject": None,
                "phase_b_searches": 0,     # populated by Phase B if it runs
                "exit_code": 1 if overall_failure else 0,
                "failure_reason": (overall_failure[0] if overall_failure else None),
            })
            conn_log.close()
        except Exception as e:
            print(f"[tracker] WARN: failed to persist tracker_runs row: {e}",
                  file=sys.stderr)

    # ---- Handle outcome ----
    if overall_failure:
        reason, exit_code, failed_year = overall_failure
        print(f"[tracker] DONE with failure — total_persisted={total_persisted}, "
              f"reason={reason}", file=sys.stderr)

        if not args.no_alert:
            html = format_error_report(
                reason=reason,
                exit_code=exit_code,
                args=last_args,
                stdout="\n---\n".join(all_stdout),
                stderr="\n---\n".join(all_stderr),
                since=since,
                year=failed_year,
                timestamp=timestamp,
            )
            subject = f"[ERROR] Data Maintenance — {failed_year}"
            send_admin_alert(subject, html, args.admin)
        else:
            print("[tracker] --no-alert: skipping admin email", file=sys.stderr)

        return 1

    # Success path — silent per spec.
    # Phase B (tracker_phase_b.py) is invoked separately by the runner
    # if the sentinel file indicates ≥1 new trade.
    print(f"[tracker] DONE — persisted={total_persisted} skipped={total_skipped} "
          f"new_trade_ids={len(new_trade_ids)} years={years}", file=sys.stderr)
    if args.dry_run:
        print("[tracker] (dry-run — nothing written to DB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
