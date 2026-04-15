#!/usr/bin/env python3
"""
format_report.py — Convert agent markdown narrative → production HTML email.

Phase 3.1 rewrite: replaces the hand-rolled markdown-to-HTML converter
with industry-standard tooling:
  - markdown2 → parses markdown with GFM-style extras (tables, strike,
    fenced code, etc.)
  - Jinja2 → template rendering with agent-specific variants
  - mjml-python → compiles the MJML template to email-client-compatible
    HTML (handles Gmail/Outlook/Apple Mail quirks via table layouts
    and inlined CSS)

Why this matters: the previous format_report.py hand-rolled inline CSS
that worked on modern webmail but rendered poorly on Outlook and some
mobile clients. MJML's entire reason for existing is to solve the email-
client compatibility problem.

CLI:
    # Backward-compatible stdin mode (defaults to daily template):
    cat narrative.md | python3 format_report.py > report.html

    # Explicit template selection:
    python3 format_report.py --template daily --in narrative.md --out report.html
    python3 format_report.py --template weekly --in narrative.md --out report.html
    python3 format_report.py --template deepdive --in narrative.md \\
        --politician "Mark Green" --roster-tier core \\
        --out report.html

    # Variables (optional, control the banner):
    --date-display "Apr 14, 2026"
    --subject "Daily Signal — Apr 14, 2026 — ..."
    --count-strong 2 --count-base 5 --count-moderate 12 --count-skip 3
    --count-flagged 14  --count-beat-spy-pct "50%"  (weekly only)

Templates live in templates/*.mjml.j2 alongside a shared _shell.mjml.j2.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional

try:
    import markdown2
    import jinja2
    import mjml
    HAS_DEPS = True
except ImportError as e:
    HAS_DEPS = False
    _IMPORT_ERR = str(e)

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"

# Subject-line patterns we can auto-parse for banner counts
# "N STRONG / N BASE / N MODERATE / N SKIP"     (daily)
# "N flagged, N% beat SPY, N STRONG"            (weekly)
# "N new filings from M politicians (...)"      (tracker)
_DAILY_SUBJECT_RE = re.compile(
    r"(\d+)\s+STRONG\s*/\s*(\d+)\s+BASE\s*/\s*(\d+)\s+MODERATE\s*/\s*(\d+)\s+SKIP"
)
_WEEKLY_SUBJECT_RE = re.compile(
    r"(\d+)\s+flagged[,\s]+(\d+%)\s+beat\s+SPY[,\s]+(\d+)\s+STRONG",
    re.IGNORECASE,
)
_TRACKER_SUBJECT_RE = re.compile(
    r"(\d+)\s+new\s+filings?\s+from\s+(\d+)\s+politicians?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Markdown → HTML (with email-safe extras)
# ---------------------------------------------------------------------------

def markdown_to_html(md_text: str) -> str:
    """
    Convert markdown text to HTML suitable for embedding inside an MJML
    <mj-text> block. We use markdown2 with the GFM-style extras most
    relevant for our narratives: tables, fenced code blocks, strikethrough,
    footnotes, task lists, and target-blank links.
    """
    extras = [
        "tables",
        "fenced-code-blocks",
        "strike",
        "target-blank-links",
        "cuddled-lists",
        "header-ids",
        "footnotes",
    ]
    html = markdown2.markdown(md_text, extras=extras)
    return html


# ---------------------------------------------------------------------------
# Subject-line parsing helpers (auto-populate banner counts)
# ---------------------------------------------------------------------------

def parse_daily_counts(subject: str) -> Dict[str, int]:
    """Extract STRONG/BASE/MODERATE/SKIP counts from the daily SUBJECT line."""
    m = _DAILY_SUBJECT_RE.search(subject or "")
    if not m:
        return {"STRONG": 0, "BASE": 0, "MODERATE": 0, "SKIP": 0}
    return {
        "STRONG": int(m.group(1)),
        "BASE": int(m.group(2)),
        "MODERATE": int(m.group(3)),
        "SKIP": int(m.group(4)),
    }


def parse_weekly_counts(subject: str) -> Dict:
    """Extract flagged/beat_spy/STRONG from the weekly SUBJECT line."""
    m = _WEEKLY_SUBJECT_RE.search(subject or "")
    if not m:
        return {"flagged": 0, "beat_spy_pct": "—", "STRONG": 0}
    return {
        "flagged": int(m.group(1)),
        "beat_spy_pct": m.group(2),
        "STRONG": int(m.group(3)),
    }


def parse_tracker_counts(subject: str) -> Dict:
    """Extract new_filings/politicians counts from the tracker SUBJECT line."""
    m = _TRACKER_SUBJECT_RE.search(subject or "")
    if not m:
        return {"new_filings": 0, "politicians": 0, "cache_miss": 0}
    return {
        "new_filings": int(m.group(1)),
        "politicians": int(m.group(2)),
        "cache_miss": 0,  # optionally populated via --count-cache-miss
    }


# ---------------------------------------------------------------------------
# Main rendering pipeline
# ---------------------------------------------------------------------------

def render_email(
    template_name: str,
    narrative_md: str,
    subject: Optional[str] = None,
    date_display: Optional[str] = None,
    politician_name: Optional[str] = None,
    roster_tier: Optional[str] = None,
    counts: Optional[Dict] = None,
    preview_text: Optional[str] = None,
    charts_sidecar: Optional[str] = None,
) -> str:
    """
    Render a narrative markdown document → production HTML email.

    Args:
        template_name: one of "daily", "weekly", "deepdive"
        narrative_md: the agent's markdown body (WITHOUT the SUBJECT line
                      and WITHOUT any trailing JSON block — those should
                      be stripped by the caller)
        subject: the full subject line (used to auto-populate banner
                 counts if `counts` is not provided)
        date_display: "Apr 14, 2026" style
        politician_name: for deepdive banner
        roster_tier: for deepdive banner ("core" / "watchlist" / etc)
        counts: dict of banner counts; if None, parsed from subject
        preview_text: Gmail preview snippet (first ~90 chars of TL;DR)

    Returns the final HTML as a string, ready to hand to Resend.
    """
    if not HAS_DEPS:
        raise RuntimeError(
            f"Missing required dependencies: {_IMPORT_ERR}. "
            f"Install with: pip3 install --user mjml-python jinja2 markdown2"
        )

    # Derive agent label from template name for the banner
    agent_labels = {
        "daily": "Daily Signal",
        "weekly": "Weekly Deep Research",
        "deepdive": "Politician Deep-Dive",
        "tracker": "Data Maintenance",
    }
    agent_label = agent_labels.get(template_name, "Signal Report")

    # Auto-parse banner counts from subject if not explicitly given
    if counts is None:
        if template_name == "daily":
            counts = parse_daily_counts(subject or "")
        elif template_name == "weekly":
            counts = parse_weekly_counts(subject or "")
        elif template_name == "tracker":
            counts = parse_tracker_counts(subject or "")
        else:
            counts = {}

    # Derive preview text from the first paragraph of the markdown
    if not preview_text:
        # Skip headings, find first non-empty non-bullet line
        for line in narrative_md.split("\n"):
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("|"):
                continue
            if s.startswith("- ") or s.startswith("* "):
                s = s[2:]
            # Strip markdown emphasis markers for preview
            s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
            s = re.sub(r"\*(.+?)\*", r"\1", s)
            s = re.sub(r"`(.+?)`", r"\1", s)
            preview_text = s[:90].strip()
            break

    # Substitute chart placeholders from the sidecar BEFORE markdown2 runs.
    # markdown2 sees real `![alt](data:image/png;base64,...)` tags and
    # renders them as <img> tags naturally.
    if charts_sidecar:
        try:
            # Lazy-import charts so format_report still works if charts.py
            # isn't available (e.g., in a minimal install without matplotlib)
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import charts as _charts
            registry = _charts.ChartRegistry.load(charts_sidecar)
            if registry.to_dict():
                narrative_md = registry.substitute(narrative_md)
        except Exception as e:
            print(f"[format_report] WARN: chart substitution failed: {e}",
                  file=sys.stderr)

    # Convert markdown → HTML
    body_html = markdown_to_html(narrative_md)

    # Jinja2 render with FileSystemLoader so {% extends "_shell.mjml.j2" %}
    # in the template files resolves correctly
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,  # We hand-control escaping; body_html is trusted
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template_file = f"{template_name}.mjml.j2"
    try:
        template = env.get_template(template_file)
    except jinja2.TemplateNotFound:
        raise RuntimeError(
            f"Template not found: {TEMPLATES_DIR / template_file}. "
            f"Available templates: {', '.join(env.list_templates())}"
        )

    mjml_source = template.render(
        subject=subject,
        preview_text=preview_text or "",
        date_display=date_display or "",
        agent_label=agent_label,
        politician_name=politician_name,
        roster_tier=roster_tier,
        counts=counts,
        body_html=body_html,
    )

    # Compile MJML → HTML
    html = mjml.mjml2html(mjml_source)

    if not html:
        raise RuntimeError(
            "MJML compilation returned empty output. "
            "Check templates/ for syntax errors."
        )
    return html


# ---------------------------------------------------------------------------
# Backward-compat: markdown_to_html as a top-level function
# (some legacy callers import this directly)
# ---------------------------------------------------------------------------

# Already defined above. Alias for legacy compatibility.
def legacy_markdown_to_html(text: str) -> str:
    """Legacy entry point — renders as daily template with minimal metadata."""
    return render_email(
        template_name="daily",
        narrative_md=text,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_counts_from_args(args, template_name: str) -> Optional[Dict]:
    if template_name == "daily":
        if any(getattr(args, k) is not None for k in
               ("count_strong", "count_base", "count_moderate", "count_skip")):
            return {
                "STRONG": args.count_strong or 0,
                "BASE": args.count_base or 0,
                "MODERATE": args.count_moderate or 0,
                "SKIP": args.count_skip or 0,
            }
    elif template_name == "weekly":
        if any(getattr(args, k) is not None for k in
               ("count_flagged", "count_beat_spy_pct", "count_strong")):
            return {
                "flagged": args.count_flagged or 0,
                "beat_spy_pct": args.count_beat_spy_pct or "—",
                "STRONG": args.count_strong or 0,
            }
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Convert agent markdown → production HTML email via MJML"
    )
    ap.add_argument(
        "--template", default="daily",
        choices=["daily", "weekly", "deepdive", "tracker"],
        help="Which MJML template to use (default: daily)",
    )
    ap.add_argument("--in", dest="input_file",
                    help="Input markdown file (default: stdin)")
    ap.add_argument("--out", dest="output_file",
                    help="Output HTML file (default: stdout)")
    ap.add_argument("--subject", help="Subject line (for auto-parsing banner counts)")
    ap.add_argument("--date-display", help='"Apr 14, 2026" style date for banner')
    ap.add_argument("--politician", help="Politician name for deepdive banner")
    ap.add_argument("--roster-tier",
                    choices=["core", "watchlist", "probationary", "candidate"],
                    help="Roster tier for deepdive banner")
    ap.add_argument("--preview-text", help="Gmail preview snippet")

    # Explicit count overrides
    ap.add_argument("--count-strong", type=int)
    ap.add_argument("--count-base", type=int)
    ap.add_argument("--count-moderate", type=int)
    ap.add_argument("--count-skip", type=int)
    ap.add_argument("--count-flagged", type=int)
    ap.add_argument("--count-beat-spy-pct", help='e.g. "50%%"')
    ap.add_argument("--charts-sidecar",
                    help="Path to a chart-registry JSON sidecar file "
                         "(see scripts/charts.py ChartRegistry). Substitutes "
                         "<!--CHART:id--> placeholders in the narrative with "
                         "base64 image tags before rendering.")
    args = ap.parse_args()

    # Read narrative
    if args.input_file:
        narrative_md = Path(args.input_file).read_text()
    else:
        narrative_md = sys.stdin.read()

    if not narrative_md.strip():
        print("[format_report] ERROR: empty input", file=sys.stderr)
        return 1

    counts = _build_counts_from_args(args, args.template)

    try:
        html = render_email(
            template_name=args.template,
            narrative_md=narrative_md,
            subject=args.subject,
            date_display=args.date_display,
            politician_name=args.politician,
            roster_tier=args.roster_tier,
            counts=counts,
            preview_text=args.preview_text,
            charts_sidecar=args.charts_sidecar,
        )
    except Exception as e:
        print(f"[format_report] ERROR: render failed: {e}", file=sys.stderr)
        return 1

    if args.output_file:
        Path(args.output_file).write_text(html)
    else:
        sys.stdout.write(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
