#!/usr/bin/env python3
"""
Send HTML email via Resend API — sends individually to each recipient for instant delivery.

CLI (backward-compatible positional form):
    python3 send_email.py "Subject" /path/to/report.html

CLI (flag form, also supports --to for single-recipient override):
    python3 send_email.py --subject "Subject" --html-file /path/to/report.html
    python3 send_email.py --subject "Subject" --html-file report.html --to me@example.com
    python3 send_email.py --subject "Subject" --html-file report.html --to a@x.com,b@y.com

--to overrides config/email-distro.json entirely. Use it for admin-only
alerts, single-politician deep-dives, or any delivery that shouldn't go
to the full distro.
"""
import argparse
import json
import sys
import os
import urllib.request
import urllib.error
import time


def send_email(subject, html_body=None, html_file=None, to_override=None):
    """
    Send one email per recipient via Resend.

    to_override: optional list of email addresses. When provided, overrides
                 the recipients list in config/email-distro.json entirely.
                 Use for admin-only alerts and on-demand reports.
    """
    if html_file and os.path.exists(html_file):
        with open(html_file) as f:
            html_body = f.read()

    if not html_body:
        print("Error: No HTML content provided")
        return False

    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'email-distro.json')
    with open(config_path) as f:
        config = json.load(f)

    if to_override:
        recipients = to_override
        print(f"  [send_email] --to override: {len(recipients)} recipient(s)")
    else:
        recipients = config['recipients']
    sender = config['reply_to']
    api_key = os.environ.get('RESEND_API_KEY', 're_GksxetM4_FrXbJJZmxCfHmBk4gm1s2wpK')

    success = 0
    failed = 0

    for recipient in recipients:
        payload = {
            "from": "APES Research <research@apesdegen.com>",
            "to": [recipient],
            "reply_to": sender,
            "subject": subject,
            "html": html_body
        }

        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            'https://api.resend.com/emails',
            data=data,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'User-Agent': 'CongressTrades/1.0'
            },
            method='POST'
        )

        try:
            response = urllib.request.urlopen(req)
            result = json.loads(response.read().decode('utf-8'))
            print(f'  Sent to {recipient} (id: {result.get("id", "?")})')
            success += 1
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ''
            print(f'  FAILED {recipient}: {e.code} - {error_body}')
            failed += 1
        except Exception as e:
            print(f'  FAILED {recipient}: {e}')
            failed += 1

        time.sleep(0.3)

    print(f'Email complete: {success} sent, {failed} failed — {subject}')
    return failed == 0


def _parse_to_list(value):
    """--to accepts a single address or comma-separated list."""
    if not value:
        return None
    return [x.strip() for x in value.split(",") if x.strip()]


if __name__ == '__main__':
    # Flag form takes precedence if any flag is present
    if any(a.startswith("--") for a in sys.argv[1:]):
        ap = argparse.ArgumentParser(description="Send HTML email via Resend")
        ap.add_argument("--subject", required=True)
        ap.add_argument("--html-file", dest="html_file")
        ap.add_argument("--html", dest="html_body")
        ap.add_argument("--to", dest="to_override",
                        help="Override distro: single email or comma-separated list")
        args = ap.parse_args()
        ok = send_email(
            args.subject,
            html_body=args.html_body,
            html_file=args.html_file,
            to_override=_parse_to_list(args.to_override),
        )
        sys.exit(0 if ok else 1)

    # Backward-compatible positional form: send_email.py "Subject" /path/to/file.html
    if len(sys.argv) >= 3:
        subject = sys.argv[1]
        if os.path.exists(sys.argv[2]):
            send_email(subject, html_file=sys.argv[2])
        else:
            send_email(subject, html_body=sys.argv[2])
    else:
        print('Usage:')
        print('  python3 send_email.py "Subject" /path/to/report.html')
        print('  python3 send_email.py --subject "Subject" --html-file report.html [--to me@example.com]')
