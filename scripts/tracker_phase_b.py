#!/usr/bin/env python3
"""
tracker_phase_b.py — Phase B driver for the Data Maintenance agent.

Phase 2.4: runs AFTER scripts/data_maintenance.py writes a sentinel file
indicating ≥1 new trade was ingested. This wrapper:

  1. Reads the sentinel JSON to learn which trade IDs are new
  2. Queries the trades table for full metadata on each
  3. Looks up any existing committee assignments for each politician
     from politicians.committee_history (populated by prior tracker runs)
  4. Writes a research pack listing each new filing with committee
     annotations — "not on a committee" when the politician is unknown
  5. The runner (run-agent.sh tracker) invokes claude --print with
     prompts/tracker.md so Sonnet 4.6 can do fresh web searches for
     politicians that lack cached committee data, compose the email,
     and emit a fenced JSON block of cache updates
  6. The runner's Phase C writeback step reads the JSON and updates
     politicians.committee_history so future runs skip the search

Invocation:
    python3 scripts/tracker_phase_b.py \\
        --sentinel outputs/tmp/tracker_sentinel_TIMESTAMP.json \\
        --out outputs/tmp/tracker_TIMESTAMP_pack.md

    # Writeback mode (called by runner after Sonnet composes the narrative):
    python3 scripts/tracker_phase_b.py --writeback outputs/tmp/tracker_TIMESTAMP_narrative.md

Outputs:
    --out PATH: markdown research pack for Sonnet to read
    Plus: updates to politicians.committee_history via writeback
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
TMP_DIR = BASE_DIR / "outputs" / "tmp"

# Reuse the deepdive canonicalization so politician names are consistent
try:
    from ingest import canonical_politician_name  # noqa: E402
except Exception:
    def canonical_politician_name(name: str) -> str:
        return (name or "").strip()


# ---------------------------------------------------------------------------
# Committee cache lookup
# ---------------------------------------------------------------------------

def get_cached_committees(conn, politician_name: str) -> Optional[List[Dict]]:
    """
    Look up any cached committee assignments for this politician from
    politicians.committee_history (a JSON column).

    Returns a list of {committee, chamber, position?} dicts, or None if
    the politician isn't in the DB or has no cached data.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT committee_history FROM politicians WHERE name = ? LIMIT 1",
        (politician_name,),
    )
    row = cur.fetchone()
    if not row or not row["committee_history"]:
        return None
    try:
        parsed = json.loads(row["committee_history"])
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def update_committee_cache(
    conn,
    politician_name: str,
    committees: List[Dict],
    chamber: str = "house",
) -> None:
    """
    Write (or update) committee_history for a politician. Upserts the
    politicians row if needed. Called during the writeback phase after
    Sonnet returns committee findings.
    """
    committee_json = json.dumps(committees)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO politicians (name, chamber, committee_history, last_updated)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name, chamber) DO UPDATE SET
            committee_history = excluded.committee_history,
            last_updated = excluded.last_updated
        """,
        (
            politician_name,
            chamber,
            committee_json,
            datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Pack rendering
# ---------------------------------------------------------------------------

def fetch_trade_details(conn, trade_ids: List[int]) -> List[Dict]:
    """Pull full rows for the given trade_ids, preserving order."""
    if not trade_ids:
        return []
    placeholders = ",".join("?" * len(trade_ids))
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id, politician_name, ticker, trade_date, disclosure_date,
               transaction_type, amount_range, trader_tag, sector
        FROM trades
        WHERE id IN ({placeholders})
        ORDER BY disclosure_date DESC, trade_date DESC
        """,
        trade_ids,
    )
    return [dict(r) for r in cur.fetchall()]


def group_by_politician(trades: List[Dict]) -> Dict[str, List[Dict]]:
    """Bucket trades by politician_name for the "filings per politician" view."""
    grouped: Dict[str, List[Dict]] = {}
    for t in trades:
        name = canonical_politician_name(t.get("politician_name") or "")
        if not name:
            continue
        grouped.setdefault(name, []).append(t)
    return grouped


def render_pack(
    conn,
    trades: List[Dict],
    run_timestamp: str,
    since_date: Optional[str],
) -> Dict:
    """
    Build the research pack markdown for Sonnet Phase B.

    Structure:
      - Metadata header (timestamp, N new filings, since_date)
      - Per-politician section with:
        * Name
        * Cached committee assignments (if any)
        * List of new filings (ticker, type, amount, trade/disclosure dates)
        * A note for Sonnet: "committee assignments unknown — do 2-3 web
          searches" when cache is empty

    Returns dict with:
      - pack: markdown string
      - politicians_needing_search: list of names Sonnet needs to research
      - politicians_with_cache: list of {name, committees} already cached
    """
    lines: List[str] = []
    grouped = group_by_politician(trades)
    politicians_needing_search: List[str] = []
    politicians_with_cache: List[Dict] = []

    total_politicians = len(grouped)
    total_filings = len(trades)

    lines.append(f"# Tracker Phase B — Research Pack ({run_timestamp})\n")
    lines.append(
        f"*This run ingested {total_filings} new filings from "
        f"{total_politicians} politicians since `{since_date or 'unknown'}`.*\n"
    )

    lines.append("## New filings by politician\n")
    lines.append(
        "For each politician below, the pack shows any cached committee "
        "assignments. Politicians marked **\"cache miss\"** need 2–3 web "
        "searches to discover their current (119th Congress) committee "
        "assignments. Do NOT search the cached politicians — just use "
        "the data below verbatim. Budget: 30 web searches max across all "
        "cache-miss politicians.\n"
    )

    for politician_name in sorted(grouped.keys()):
        trades_for_this = grouped[politician_name]
        cached = get_cached_committees(conn, politician_name)

        lines.append(f"### {politician_name}\n")

        if cached:
            lines.append("**Committee assignments (cached):**")
            for c in cached:
                committee = c.get("committee") or "unknown"
                chamber = c.get("chamber") or ""
                position = c.get("position") or ""
                extra = f" — {position}" if position else ""
                chamber_str = f" ({chamber})" if chamber else ""
                lines.append(f"- {committee}{chamber_str}{extra}")
            politicians_with_cache.append({
                "name": politician_name,
                "committees": cached,
            })
        else:
            lines.append("**Committee assignments:** *cache miss — please research*")
            politicians_needing_search.append(politician_name)

        lines.append(f"\n**New filings ({len(trades_for_this)}):**")
        lines.append("| Ticker | Type | Amount | Trade Date | Disclosure |")
        lines.append("|---|---|---|---|---|")
        for t in trades_for_this:
            lines.append(
                f"| {t.get('ticker') or '—'} "
                f"| {t.get('transaction_type') or '—'} "
                f"| {t.get('amount_range') or '—'} "
                f"| {t.get('trade_date') or '—'} "
                f"| {t.get('disclosure_date') or '—'} |"
            )
        lines.append("")

    lines.append("---")
    lines.append(
        f"**Cache status:** {len(politicians_with_cache)} cached, "
        f"{len(politicians_needing_search)} need research. "
        "Use web search for only the cache-miss politicians."
    )

    return {
        "pack": "\n".join(lines) + "\n",
        "politicians_needing_search": politicians_needing_search,
        "politicians_with_cache": politicians_with_cache,
    }


# ---------------------------------------------------------------------------
# Writeback: parse Sonnet's JSON block and update committee cache
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```json\s*\n(\{.*?\})\s*\n```", re.DOTALL)


def extract_json_block(narrative: str) -> Optional[Dict]:
    """
    Find the final fenced ```json block in Sonnet's narrative. The prompt
    instructs Sonnet to emit one JSON block with committee cache updates
    and an email_metadata section (subject, search count).
    """
    matches = _JSON_FENCE_RE.findall(narrative)
    if not matches:
        return None
    for block in reversed(matches):
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def apply_writeback(conn, narrative_path: str) -> Dict:
    """
    Read Sonnet's narrative, extract the JSON block, and update
    politicians.committee_history for each researched politician.

    Returns summary dict: {parsed, committee_updates, email_subject,
    searches, errors}.
    """
    summary: Dict = {
        "parsed": False,
        "committee_updates": 0,
        "email_subject": None,
        "searches": 0,
        "errors": [],
    }

    try:
        with open(narrative_path) as f:
            narrative = f.read()
    except OSError as e:
        summary["errors"].append(f"read failed: {e}")
        return summary

    parsed = extract_json_block(narrative)
    if not parsed:
        summary["errors"].append("no fenced JSON block found")
        return summary

    summary["parsed"] = True

    # Committee cache updates
    cache_updates = parsed.get("committee_updates", [])
    if isinstance(cache_updates, list):
        for entry in cache_updates:
            if not isinstance(entry, dict):
                continue
            name = entry.get("politician") or entry.get("name")
            committees = entry.get("committees")
            chamber = entry.get("chamber") or "house"
            if not name or not isinstance(committees, list):
                continue
            try:
                update_committee_cache(conn, name, committees, chamber=chamber)
                summary["committee_updates"] += 1
            except Exception as e:
                summary["errors"].append(f"update failed for {name}: {e}")

    # Email metadata
    meta = parsed.get("email_metadata") or {}
    if isinstance(meta, dict):
        summary["email_subject"] = meta.get("subject")
        summary["searches"] = int(meta.get("searches_used") or 0)

    return summary


def update_tracker_run_after_email(
    conn,
    run_timestamp: str,
    email_sent: bool,
    email_subject: Optional[str] = None,
    searches: int = 0,
) -> None:
    """
    Update the tracker_runs row written by data_maintenance.py after
    Phase B finishes, so the audit log reflects whether the email was
    composed and sent.
    """
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE tracker_runs
        SET email_sent = ?,
            email_subject = ?,
            phase_b_searches = ?
        WHERE run_timestamp = ?
        """,
        (
            1 if email_sent else 0,
            email_subject,
            searches,
            run_timestamp,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Tracker Phase B: pack prep + writeback")
    ap.add_argument("--sentinel", help="Path to the sentinel JSON from data_maintenance.py")
    ap.add_argument("--out", help="Output path for the Phase B research pack markdown")
    ap.add_argument("--writeback",
                    help="Path to the Sonnet narrative file; parses JSON block "
                         "and updates politicians.committee_history. Mutually "
                         "exclusive with --sentinel.")
    args = ap.parse_args()

    conn = db.connect()

    # ---- Writeback mode ----
    if args.writeback:
        print(f"[tracker-phase-b] writeback mode — reading {args.writeback}",
              file=sys.stderr)
        summary = apply_writeback(conn, args.writeback)
        print(f"[tracker-phase-b] parsed={summary['parsed']} "
              f"committee_updates={summary['committee_updates']} "
              f"searches={summary['searches']} "
              f"subject={summary['email_subject']}",
              file=sys.stderr)
        if summary["errors"]:
            print(f"[tracker-phase-b] errors:", file=sys.stderr)
            for err in summary["errors"][:10]:
                print(f"  - {err}", file=sys.stderr)
        # Dump the summary so the runner can pick up the subject + search count
        print(json.dumps(summary, default=str))
        conn.close()
        return 0 if summary["parsed"] else 1

    # ---- Pack-prep mode ----
    if not args.sentinel:
        print("[tracker-phase-b] ERROR: --sentinel or --writeback required",
              file=sys.stderr)
        return 2

    try:
        with open(args.sentinel) as f:
            sentinel = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"[tracker-phase-b] FATAL: could not read sentinel: {e}",
              file=sys.stderr)
        return 2

    new_trade_ids = sentinel.get("new_trade_ids") or []
    if not new_trade_ids:
        print("[tracker-phase-b] no new trades in sentinel — exiting without work",
              file=sys.stderr)
        return 0

    trades = fetch_trade_details(conn, new_trade_ids)
    result = render_pack(
        conn, trades,
        run_timestamp=sentinel.get("run_timestamp") or datetime.utcnow().isoformat(),
        since_date=sentinel.get("since_date"),
    )

    print(f"[tracker-phase-b] pack ready: {len(trades)} trades, "
          f"{len(result['politicians_needing_search'])} politicians need search, "
          f"{len(result['politicians_with_cache'])} cached",
          file=sys.stderr)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result["pack"])
        print(f"[tracker-phase-b] wrote pack: {out_path} "
              f"({len(result['pack']):,} bytes)",
              file=sys.stderr)
    else:
        sys.stdout.write(result["pack"])

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
