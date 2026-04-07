#!/usr/bin/env python3
"""
ingest.py — Multi-source congressional trade ingestion.

Pulls disclosures from:
  1. House eFD XML archive  (disclosures-clerk.house.gov)  — primary, deterministic
  2. Finnhub API            (free tier, congressional-trading endpoint) — reconciliation
  3. Capitol Trades         — handled by agent prompts via WebFetch (NOT in this script)

Reconciles + dedupes by (politician_normalized, ticker, trade_date, transaction_type)
and writes to the trades table via db.upsert_trade.

Usage:
    python3 scripts/ingest.py --source house-efd --year 2026
    python3 scripts/ingest.py --source finnhub --since 2026-03-01
    python3 scripts/ingest.py --all                          # all sources, last 7 days
    python3 scripts/ingest.py --all --since 2026-01-01       # all sources, custom range
    python3 scripts/ingest.py --dry-run --source finnhub --since 2026-03-01

Network access required. Set FINNHUB_API_KEY in env or config/finnhub.json.
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import db

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
TMP_DIR = BASE_DIR / "outputs" / "tmp"
USER_AGENT = "CongressTradesBot/1.0 (research; contact: research@apesdegen.com)"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_finnhub_key() -> Optional[str]:
    if "FINNHUB_API_KEY" in os.environ:
        return os.environ["FINNHUB_API_KEY"]
    cfg = CONFIG_DIR / "finnhub.json"
    if cfg.exists():
        with open(cfg) as f:
            return json.load(f).get("api_key")
    return None


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int = 30, retries: int = 3) -> bytes:
    """Polite GET with backoff. Returns raw bytes."""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  [retry {attempt+1}/{retries}] {e} — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"HTTP GET failed after {retries} retries: {url} ({last_err})")


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

# Politician name normalization
# Handles the ugly cases observed in real House eFD data:
#   "Hon. Richard W. Allen"           -> "Richard Allen"
#   "Scott Scott Franklin"            -> "Scott Franklin"    (duplicated first)
#   "C. Scott Franklin"               -> "Scott Franklin"    (initial prefix)
#   "Scott Mr Franklin"               -> "Scott Franklin"    (mid honorific)
#   "Donald Sternoff Honorable Beyer" -> "Donald Sternoff Beyer"
#   "Marjorie Taylor Mrs Greene"      -> "Marjorie Taylor Greene"
#   "Mark Dr Green"                   -> "Mark Green"
#   "James E Hon Banks"               -> "James Banks"
# Real middle names (like Taylor in Marjorie Taylor Greene) are preserved.

_HONORIFICS = {
    "mr", "mrs", "ms", "miss", "dr", "hon", "honorable",
    "rep", "sen", "representative", "senator", "rev", "sir", "the",
}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "esq", "phd", "md", "facs", "dds"}
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'.\-]*")


def _title_token(t: str) -> str:
    """
    Title-case a single name token. Preserves intentional mid-word capitals
    like 'DesJarlais', 'LaHood', 'McDonald', 'MacKenzie' by only title-casing
    when the input is all-lowercase or all-uppercase.
    """
    if not t:
        return t
    # Preserve tokens that already have intentional mixed case
    if t != t.lower() and t != t.upper():
        return t
    low = t.lower()
    if low.startswith("mc") and len(t) > 2:
        return "Mc" + t[2:].capitalize()
    if "'" in t:
        return "'".join(p.capitalize() for p in t.split("'"))
    if "-" in t:
        return "-".join(p.capitalize() for p in t.split("-"))
    return t.capitalize()


def canonical_politician_name(raw: str) -> str:
    """
    Return a canonical 'First [Middle...] Last' form with honorifics stripped,
    single-letter initials dropped, and duplicate-adjacent tokens collapsed.
    """
    if not raw:
        return ""
    n = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode()
    tokens = _NAME_TOKEN_RE.findall(n)

    cleaned = []
    for tok in tokens:
        low = tok.lower().rstrip(".")
        if low in _HONORIFICS or low in _SUFFIXES:
            continue
        # Drop single-letter tokens (middle initials with or without period)
        if len(low) <= 1:
            continue
        cleaned.append(tok.rstrip("."))

    # Dedupe consecutive duplicate tokens (handles "Scott Scott Franklin")
    deduped = []
    for tok in cleaned:
        if deduped and deduped[-1].lower() == tok.lower():
            continue
        deduped.append(tok)

    return " ".join(_title_token(t) for t in deduped)


def normalize_name(name: str) -> str:
    """Lowercase form of canonical name — used as the dedup key."""
    return canonical_politician_name(name).lower()


def normalize_transaction_type(raw: str) -> str:
    if not raw:
        return "unknown"
    r = raw.strip().lower()
    if any(w in r for w in ["purchase", "buy", "p"]):
        if r in ("p", "purchase"):
            return "buy"
        if "buy" in r or "purchase" in r:
            return "buy"
    if any(w in r for w in ["sale", "sell", "s"]):
        if r in ("s", "sale", "sell", "sf", "sp"):
            return "sell"
        if "sell" in r or "sale" in r:
            return "sell"
    if "exchange" in r:
        return "exchange"
    return r


def normalize_trader_tag(owner_code: Optional[str]) -> str:
    """House eFD owner codes: SP=spouse, DC=dependent, JT=joint, others=member."""
    if not owner_code:
        return "member_direct"
    code = owner_code.strip().upper()
    if code in ("SP",):
        return "spouse"
    if code in ("DC",):
        return "dependent"
    return "member_direct"


def make_trade_key(politician: str, ticker: str, trade_date: str, txn_type: str) -> str:
    return f"{normalize_name(politician)}|{(ticker or '').upper()}|{trade_date}|{txn_type}"


# ---------------------------------------------------------------------------
# Source 1: House eFD
# ---------------------------------------------------------------------------

HOUSE_EFD_INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_EFD_PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
PDF_CACHE = BASE_DIR / "outputs" / "tmp" / "house_efd_pdfs"


def fetch_house_efd_index(year: int) -> List[Dict]:
    """
    Download the House eFD year ZIP, parse the included XML index of all filings.
    Returns a list of {DocID, FilingType, Last, First, FilingDate, ...} dicts.

    Note: This index lists FILINGS, not individual trades. Trade-level data lives
    inside the linked PDFs. PDF parsing is deferred — we use this index to identify
    which members filed in the window so the agent can cross-reference Capitol Trades.
    """
    url = HOUSE_EFD_INDEX_URL.format(year=year)
    print(f"[house-efd] fetching {url}")
    data = http_get(url)

    filings = []
    with zipfile.ZipFile(BytesIO(data)) as zf:
        xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
        if not xml_names:
            raise RuntimeError(f"No XML in {url}")
        for xname in xml_names:
            with zf.open(xname) as f:
                tree = ET.parse(f)
                root = tree.getroot()
                # Tag is typically <Member>
                for member in root.findall(".//Member"):
                    rec = {child.tag: (child.text or "").strip() for child in member}
                    filings.append(rec)
    print(f"[house-efd] parsed {len(filings)} filings from {year}")
    return filings


def fetch_house_efd_pdf(year: int, doc_id: str, cache: bool = True) -> Optional[bytes]:
    """Download a single PTR PDF (with on-disk cache)."""
    PDF_CACHE.mkdir(parents=True, exist_ok=True)
    cached = PDF_CACHE / f"{year}_{doc_id}.pdf"
    if cache and cached.exists():
        return cached.read_bytes()
    url = HOUSE_EFD_PDF_URL.format(year=year, doc_id=doc_id)
    try:
        data = http_get(url, timeout=20, retries=2)
        if cache:
            cached.write_bytes(data)
        return data
    except Exception as e:
        print(f"  [pdf miss] {doc_id}: {e}", file=sys.stderr)
        return None


# PTR parser v2 — line-based, handles multi-line wraps.
#
# PTR row layouts (observed in real filings):
#   Layout A (single line):
#     "SP Albemarle Corporation (ALB) [ST] S 12/21/2023 01/08/2024 $1,001 - $15,000"
#   Layout B (amount wraps to next line):
#     "SP Charles Schwab Corporation (SCHW) P 12/14/2023 01/08/2024 $50,001 -"
#     "[ST] $100,000"
#   Layout C (asset name wraps, ticker on line 2):
#     "SP Ameriprise Financial, Inc. Common P 04/09/2024 05/09/2024 $50,001 -"
#     "Stock (AMP) [ST] $100,000"
#   Layout D (asset name wraps, amount fully on line 1):
#     "SP Cintas Corporation - Common Stock P 05/20/2024 06/07/2024 $1,001 - $15,000"
#     "(CTAS) [ST]"
#
# Strategy: find "transaction lines" (P/S/E + two dates + $amount), then search
# the current line and adjacent lines for the ticker in parens and owner code.

PTR_TXN_LINE_RE = re.compile(
    r"\s(?P<txn>P|S\s?\(partial\)|S|E)\s+"
    r"(?P<trade_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notif_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amount>\$[\d,]+\s*-\s*\$?[\d,]*|\$[\d,]+\s*\+|\$[\d,]+)"
)
PTR_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,5})\)")
PTR_OWNER_RE = re.compile(r"^\s*(SP|JT|DC)\b")
PTR_HEADER_PHRASES = ("ID Owner Asset Transaction", "Type Date Gains", "$200?")


def parse_ptr_pdf(pdf_bytes: bytes, doc_meta: Dict) -> List[Dict]:
    """
    Parse a House eFD Periodic Transaction Report PDF into individual trades.
    See module-level comment for supported row layouts.
    """
    if not HAS_PDFPLUMBER:
        return []

    politician = f"{doc_meta.get('First','')} {doc_meta.get('Last','')}".strip()
    filing_date = doc_meta.get("FilingDate", "")
    try:
        filing_iso = datetime.strptime(filing_date, "%m/%d/%Y").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        filing_iso = filing_date

    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        print(f"  [pdf parse error] {doc_meta.get('DocID')}: {e}", file=sys.stderr)
        return []

    # Keep non-empty lines in order; strip trailing whitespace only
    lines = [ln.rstrip() for ln in text.split("\n") if ln.strip()]

    trades: List[Dict] = []
    for i, line in enumerate(lines):
        if any(p in line for p in PTR_HEADER_PHRASES):
            continue
        m = PTR_TXN_LINE_RE.search(line)
        if not m:
            continue

        # Ticker: check this line, previous line, next line
        ticker = None
        context_lines = [
            line,
            lines[i - 1] if i > 0 else "",
            lines[i + 1] if i + 1 < len(lines) else "",
        ]
        for candidate in context_lines:
            tm = PTR_TICKER_RE.search(candidate)
            if tm:
                ticker = tm.group(1)
                break
        if not ticker:
            continue  # likely a Treasury bill (no ticker in parens)

        # Owner code (SP / JT / DC) — check this line and the line above
        owner_code = None
        for candidate in (line, lines[i - 1] if i > 0 else ""):
            om = PTR_OWNER_RE.match(candidate)
            if om:
                owner_code = om.group(1)
                break

        # Stitch wrap-around amount: if amount ends with "-", append next $amount
        amount = m.group("amount").strip()
        if amount.endswith("-") and i + 1 < len(lines):
            next_amt_m = re.search(r"\$[\d,]+", lines[i + 1])
            if next_amt_m:
                amount = amount + " " + next_amt_m.group(0)

        # Trade date ISO
        try:
            trade_date_iso = datetime.strptime(m.group("trade_date"), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            continue

        txn_raw = m.group("txn").strip().upper()
        if txn_raw == "P":
            txn_type = "buy"
        elif "S" in txn_raw:
            txn_type = "sell"
        elif txn_raw == "E":
            txn_type = "exchange"
        else:
            txn_type = "unknown"

        trades.append({
            "politician": politician,
            "chamber": "house",
            "trade_date": trade_date_iso,
            "disclosure_date": filing_iso,
            "ticker": ticker,
            "transaction_type": txn_type,
            "amount_range": amount,
            "trader_tag": normalize_trader_tag(owner_code),
            "source": "house_efd",
            "source_id": doc_meta.get("DocID"),
            "asset_type": "stock",
        })

    return trades


def house_efd_filings_to_trades(filings: List[Dict]) -> List[Dict]:
    """
    Transform House eFD filing index records into placeholder trade rows.
    NOTE: The index does not contain individual transactions — only filing metadata.
    We emit one row per PTR (Periodic Transaction Report) so downstream processes know
    which member filed in the window and can resolve actual trades from Capitol Trades / Finnhub.

    For Phase 0 we treat these as 'pending' marker rows that the agent will enrich.
    """
    trades = []
    for f in filings:
        filing_type = (f.get("FilingType") or "").upper()
        # Only PTR = Periodic Transaction Report (the actual trade disclosures)
        if filing_type != "P":
            continue
        first = f.get("First", "")
        last = f.get("Last", "")
        full = f"{first} {last}".strip()
        filing_date = f.get("FilingDate") or ""
        try:
            iso_date = datetime.strptime(filing_date, "%m/%d/%Y").strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            iso_date = filing_date
        trades.append({
            "politician": full,
            "chamber": "house",
            "trade_date": iso_date,
            "disclosure_date": iso_date,
            "ticker": "_PTR_",  # placeholder — agent must resolve
            "transaction_type": "unknown",
            "amount_range": None,
            "trader_tag": "member_direct",
            "source": "house_efd",
            "source_id": f.get("DocID"),
            "asset_type": "stock",
            "_filing_type": filing_type,
            "_raw": f,
        })
    return trades


# ---------------------------------------------------------------------------
# Source 2: Finnhub
# ---------------------------------------------------------------------------

FINNHUB_BASE = "https://finnhub.io/api/v1"


def fetch_finnhub_trades(
    api_key: str,
    symbol: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> List[Dict]:
    """
    Finnhub congressional-trading endpoint.

    Free tier: by symbol only. Returns trades for a single ticker in date range.
    Without a symbol, this returns 403/empty on free tier — handle gracefully.
    """
    params = []
    if symbol:
        params.append(f"symbol={symbol}")
    if since:
        params.append(f"from={since}")
    if until:
        params.append(f"to={until}")
    params.append(f"token={api_key}")
    url = f"{FINNHUB_BASE}/stock/congressional-trading?{'&'.join(params)}"

    try:
        raw = http_get(url)
    except RuntimeError as e:
        print(f"[finnhub] failed: {e}", file=sys.stderr)
        return []

    try:
        payload = json.loads(raw.decode())
    except json.JSONDecodeError:
        print(f"[finnhub] non-JSON response: {raw[:200]!r}", file=sys.stderr)
        return []

    items = payload.get("data", []) if isinstance(payload, dict) else []
    print(f"[finnhub] {symbol or 'no-symbol'}: {len(items)} trades")

    out = []
    for it in items:
        out.append({
            "politician": it.get("name", ""),
            "chamber": "unknown",
            "trade_date": it.get("transactionDate"),
            "disclosure_date": it.get("filingDate"),
            "ticker": (it.get("symbol") or "").upper(),
            "transaction_type": normalize_transaction_type(it.get("transactionType", "")),
            "amount_range": it.get("amount"),
            "trader_tag": normalize_trader_tag(it.get("ownerType")),
            "source": "finnhub",
            "source_id": it.get("position"),
            "asset_type": it.get("assetType", "stock"),
            "_raw": it,
        })
    return out


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def reconcile(*sources: List[Dict]) -> List[Dict]:
    """
    Merge multiple sources into a deduped list. Dedupe key:
        normalize_name(politician) | ticker | trade_date | transaction_type
    Trades present in multiple sources have their `source` field merged.
    """
    by_key: Dict[str, Dict] = {}
    for src in sources:
        for t in src:
            if not t.get("ticker") or not t.get("politician") or not t.get("trade_date"):
                continue
            key = make_trade_key(t["politician"], t["ticker"], t["trade_date"], t["transaction_type"])
            if key in by_key:
                existing = by_key[key]
                if t["source"] not in existing["source"]:
                    existing["source"] += f",{t['source']}"
                if not existing.get("amount_range") and t.get("amount_range"):
                    existing["amount_range"] = t["amount_range"]
                if not existing.get("disclosure_date") and t.get("disclosure_date"):
                    existing["disclosure_date"] = t["disclosure_date"]
            else:
                by_key[key] = dict(t)
    return list(by_key.values())


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def persist(conn, trades: List[Dict], dry_run: bool = False) -> Tuple[int, int]:
    """Upsert trades into DB. Returns (inserted_or_updated, skipped_placeholders)."""
    n_ok = 0
    n_skip = 0
    for t in trades:
        if t.get("ticker") in ("_PTR_", "", None):
            n_skip += 1
            continue
        if dry_run:
            n_ok += 1
            continue
        try:
            db.upsert_trade(
                conn,
                politician_name=canonical_politician_name(t["politician"]),
                ticker=t["ticker"],
                trade_date=t["trade_date"],
                transaction_type=t["transaction_type"],
                amount_range=t.get("amount_range"),
                disclosure_date=t.get("disclosure_date"),
                asset_type=t.get("asset_type", "stock"),
                trader_tag=t.get("trader_tag", "member_direct"),
                source=t.get("source", "unknown"),
                source_id=str(t.get("source_id") or ""),
            )
            n_ok += 1
        except Exception as e:
            print(f"  [persist error] {t.get('politician')} {t.get('ticker')}: {e}", file=sys.stderr)
    return n_ok, n_skip


def _filing_in_window(filing_date_str: Optional[str], since: Optional[str], until: Optional[str]) -> bool:
    if not filing_date_str:
        return True
    try:
        d = datetime.strptime(filing_date_str, "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return True
    if since:
        try:
            s = datetime.strptime(since, "%Y-%m-%d").date()
            if d < s:
                return False
        except ValueError:
            pass
    if until:
        try:
            u = datetime.strptime(until, "%Y-%m-%d").date()
            if d > u:
                return False
        except ValueError:
            pass
    return True


def archive_raw(conn, source: str, url: str, items: List[Dict], dry_run: bool = False) -> None:
    if dry_run:
        return
    snapshot = json.dumps(items[:50], default=str)
    db.log_raw_source(conn, source=source, request_url=url, raw_response=snapshot, parsed_count=len(items))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Multi-source trade ingestion")
    p.add_argument("--source", choices=["house-efd", "finnhub"], help="Single source")
    p.add_argument("--all", action="store_true", help="All available sources")
    p.add_argument("--since", help="ISO date (YYYY-MM-DD)")
    p.add_argument("--until", help="ISO date (YYYY-MM-DD)")
    p.add_argument("--year", type=int, help="House eFD year")
    p.add_argument("--symbol", help="Finnhub symbol filter")
    p.add_argument("--symbols-from-overrides", action="store_true",
                   help="Loop Finnhub over the mega-cap override list")
    p.add_argument("--parse-pdfs", action="store_true",
                   help="For House eFD: download and parse PTR PDFs (slow, full data)")
    p.add_argument("--max-pdfs", type=int, default=50,
                   help="Cap PDFs parsed per run (default 50)")
    p.add_argument("--dry-run", action="store_true", help="Don't write to DB")
    args = p.parse_args()

    if not args.source and not args.all:
        p.print_help()
        return

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    conn = db.connect()
    finnhub_key = load_finnhub_key()

    # Default time window
    if not args.since:
        args.since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    if not args.until:
        args.until = datetime.utcnow().strftime("%Y-%m-%d")
    if not args.year:
        args.year = datetime.utcnow().year

    house_trades: List[Dict] = []
    finnhub_trades: List[Dict] = []

    if args.source == "house-efd" or args.all:
        try:
            filings = fetch_house_efd_index(args.year)
            placeholder_trades = house_efd_filings_to_trades(filings)
            archive_raw(conn, "house_efd_index", HOUSE_EFD_INDEX_URL.format(year=args.year), filings, args.dry_run)

            if args.parse_pdfs:
                if not HAS_PDFPLUMBER:
                    print("[house-efd] pdfplumber not installed — pip install pdfplumber", file=sys.stderr)
                else:
                    # Filter to PTRs in date window
                    ptrs = [
                        f for f in filings
                        if (f.get("FilingType") or "").upper() == "P" and f.get("DocID")
                    ]
                    if args.since:
                        ptrs = [
                            f for f in ptrs
                            if _filing_in_window(f.get("FilingDate"), args.since, args.until)
                        ]
                    ptrs = ptrs[: args.max_pdfs]
                    print(f"[house-efd] parsing {len(ptrs)} PTR PDFs (max {args.max_pdfs})")
                    parsed = 0
                    for i, f in enumerate(ptrs):
                        pdf_bytes = fetch_house_efd_pdf(args.year, f["DocID"])
                        if not pdf_bytes:
                            continue
                        rows = parse_ptr_pdf(pdf_bytes, f)
                        house_trades.extend(rows)
                        parsed += 1
                        if (i + 1) % 10 == 0:
                            print(f"  [pdf {i+1}/{len(ptrs)}] {len(house_trades)} trades extracted so far")
                        time.sleep(0.3)
                    print(f"[house-efd] parsed {parsed} PDFs → {len(house_trades)} trade rows")
            else:
                house_trades = placeholder_trades
        except Exception as e:
            print(f"[house-efd] FAILED: {e}", file=sys.stderr)

    if args.source == "finnhub" or args.all:
        if not finnhub_key:
            print("[finnhub] no API key (set FINNHUB_API_KEY or config/finnhub.json), skipping", file=sys.stderr)
        else:
            symbols = []
            if args.symbol:
                symbols = [args.symbol]
            elif args.symbols_from_overrides:
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT ticker FROM committee_mappings WHERE multi_jurisdiction = 1")
                symbols = [r["ticker"] for r in cur.fetchall()]
            else:
                # Default: just probe a couple of widely-traded names
                symbols = ["NVDA", "MSFT", "AAPL"]

            for sym in symbols:
                trades = fetch_finnhub_trades(finnhub_key, symbol=sym, since=args.since, until=args.until)
                finnhub_trades.extend(trades)
                time.sleep(0.5)  # rate limit politeness

            archive_raw(conn, "finnhub", FINNHUB_BASE + "/stock/congressional-trading", finnhub_trades, args.dry_run)

    print(f"\n[ingest] reconciling: house={len(house_trades)} finnhub={len(finnhub_trades)}")
    merged = reconcile(house_trades, finnhub_trades)
    print(f"[ingest] reconciled: {len(merged)} unique rows")

    n_ok, n_skip = persist(conn, merged, dry_run=args.dry_run)
    print(f"[ingest] persisted: {n_ok} trades, {n_skip} placeholders skipped")
    if args.dry_run:
        print("[ingest] (dry-run — nothing written)")

    conn.close()


if __name__ == "__main__":
    main()
