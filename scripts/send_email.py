#!/usr/bin/env python3
"""Send HTML email via Resend API — sends individually to each recipient for instant delivery."""
import json
import sys
import os
import urllib.request
import urllib.error
import time


def send_email(subject, html_body=None, html_file=None):
    if html_file and os.path.exists(html_file):
        with open(html_file) as f:
            html_body = f.read()

    if not html_body:
        print("Error: No HTML content provided")
        return False

    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config', 'email-distro.json')
    with open(config_path) as f:
        config = json.load(f)

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


if __name__ == '__main__':
    if len(sys.argv) >= 3:
        subject = sys.argv[1]
        if os.path.exists(sys.argv[2]):
            send_email(subject, html_file=sys.argv[2])
        else:
            send_email(subject, html_body=sys.argv[2])
    else:
        print('Usage: python3 send_email.py "Subject" /path/to/report.html')
