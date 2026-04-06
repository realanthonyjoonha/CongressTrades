#!/usr/bin/env python3
"""Convert Claude's raw markdown/text output into a styled HTML email report."""
import sys
import re
import html


def markdown_to_html(text):
    """Convert markdown-formatted text to styled HTML email."""
    lines = text.split('\n')
    html_parts = []
    in_table = False
    table_rows = []

    for line in lines:
        stripped = line.strip()

        if not stripped:
            if in_table:
                html_parts.append(render_table(table_rows))
                table_rows = []
                in_table = False
            html_parts.append('<div style="height:12px;"></div>')
            continue

        # Table detection
        if '|' in stripped and stripped.startswith('|'):
            in_table = True
            if re.match(r'^[\|\-\s:]+$', stripped):
                continue
            cells = [c.strip() for c in stripped.split('|')[1:-1]]
            table_rows.append(cells)
            continue
        elif in_table:
            html_parts.append(render_table(table_rows))
            table_rows = []
            in_table = False

        # Headers
        if stripped.startswith('# '):
            html_parts.append(f'<h1 style="color:#58a6ff;font-size:24px;margin:20px 0 10px 0;border-bottom:2px solid #21262d;padding-bottom:8px;">{format_inline(stripped[2:])}</h1>')
        elif stripped.startswith('## '):
            html_parts.append(f'<h2 style="color:#58a6ff;font-size:20px;margin:18px 0 8px 0;border-bottom:1px solid #21262d;padding-bottom:6px;">{format_inline(stripped[3:])}</h2>')
        elif stripped.startswith('### '):
            html_parts.append(f'<h3 style="color:#79c0ff;font-size:16px;margin:14px 0 6px 0;">{format_inline(stripped[4:])}</h3>')
        elif stripped.startswith('#### '):
            html_parts.append(f'<h4 style="color:#79c0ff;font-size:14px;margin:12px 0 4px 0;">{format_inline(stripped[5:])}</h4>')
        elif stripped in ('---', '***', '___'):
            html_parts.append('<hr style="border:none;border-top:1px solid #21262d;margin:20px 0;">')
        elif stripped.startswith('- ') or stripped.startswith('* '):
            content = stripped[2:]
            html_parts.append(f'<div style="padding:4px 0 4px 20px;">\u2022 {format_inline(content)}</div>')
        elif re.match(r'^\d+\.\s', stripped):
            content = re.sub(r'^\d+\.\s', '', stripped)
            num = re.match(r'^(\d+)', stripped).group(1)
            html_parts.append(f'<div style="padding:4px 0 4px 20px;">{num}. {format_inline(content)}</div>')
        else:
            html_parts.append(f'<p style="margin:6px 0;line-height:1.6;">{format_inline(stripped)}</p>')

    if in_table:
        html_parts.append(render_table(table_rows))

    body = '\n'.join(html_parts)

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="font-family:'Segoe UI',Arial,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:0;">
<div style="max-width:900px;margin:0 auto;padding:24px;">

<!-- Header -->
<div style="background:linear-gradient(135deg,#1a1f2e 0%,#0d1117 100%);border:1px solid #30363d;border-radius:12px;padding:28px;margin-bottom:24px;">
<div style="font-size:24px;font-weight:700;color:#58a6ff;">Congress Trades Report</div>
<div style="font-size:13px;color:#8b949e;margin-top:4px;">AI-Powered Congressional Trading Analysis</div>
</div>

<!-- Content -->
<div style="background:#161b22;border:1px solid #21262d;border-radius:12px;padding:24px;margin-bottom:24px;">
{body}
</div>

<!-- Disclaimer -->
<div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:16px;text-align:center;">
<p style="font-size:11px;color:#6e7681;">This report tracks publicly disclosed congressional stock trades filed under the STOCK Act. AI-generated for research and educational purposes only. Congressional trading data is delayed (up to 45 days from transaction to disclosure). Not financial advice. Do your own due diligence.</p>
</div>

</div>
</body>
</html>'''


def format_inline(text):
    """Convert inline markdown to HTML."""
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong style="color:#f0f6fc;">\1</strong>', text)
    text = re.sub(r'__(.+?)__', r'<strong style="color:#f0f6fc;">\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = re.sub(r'`(.+?)`', r'<code style="background:#21262d;padding:2px 6px;border-radius:4px;font-size:13px;">\1</code>', text)

    # Signal-level color codes
    text = text.replace('STRONG SIGNAL', '<span style="color:#3fb950;font-weight:700;">STRONG SIGNAL</span>')
    text = text.replace('MODERATE SIGNAL', '<span style="color:#d29922;font-weight:700;">MODERATE SIGNAL</span>')
    text = text.replace('WEAK SIGNAL', '<span style="color:#8b949e;font-weight:700;">WEAK SIGNAL</span>')
    text = text.replace('NOISE', '<span style="color:#6e7681;font-weight:700;">NOISE</span>')
    text = text.replace('HIGH CONVICTION', '<span style="color:#3fb950;font-weight:700;">HIGH CONVICTION</span>')
    text = text.replace('CAUTION', '<span style="color:#f85149;font-weight:700;">CAUTION</span>')

    # Tier labels
    text = text.replace('TIER 1', '<span style="color:#3fb950;font-weight:700;">TIER 1</span>')
    text = text.replace('TIER 2', '<span style="color:#d29922;font-weight:700;">TIER 2</span>')
    text = text.replace('TIER 3', '<span style="color:#8b949e;font-weight:700;">TIER 3</span>')

    # Transaction types
    text = text.replace('PURCHASE', '<span style="color:#3fb950;font-weight:700;">PURCHASE</span>')
    text = text.replace('SALE', '<span style="color:#f85149;font-weight:700;">SALE</span>')

    return text


def render_table(rows):
    """Render a markdown table as styled HTML."""
    if not rows:
        return ''

    html_out = '<table style="width:100%;border-collapse:collapse;margin:12px 0;font-size:13px;">'

    if rows:
        html_out += '<tr>'
        for cell in rows[0]:
            html_out += f'<th style="background:#21262d;color:#e6edf3;padding:8px 12px;text-align:left;border:1px solid #30363d;font-weight:600;">{format_inline(cell)}</th>'
        html_out += '</tr>'

    for i, row in enumerate(rows[1:]):
        bg = '#161b22' if i % 2 == 0 else '#0d1117'
        html_out += '<tr>'
        for cell in row:
            html_out += f'<td style="padding:8px 12px;border:1px solid #21262d;background:{bg};">{format_inline(cell)}</td>'
        html_out += '</tr>'

    html_out += '</table>'
    return html_out


if __name__ == '__main__':
    raw_text = sys.stdin.read()
    print(markdown_to_html(raw_text))
