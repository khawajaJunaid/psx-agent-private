"""backfill_db.py — parse historical Markdown reports and insert missing
decisions into db.sqlite.

Usage:
    python3 backfill_db.py                  # scans reports/ in current dir
    python3 backfill_db.py --dry-run        # print parsed rows, don't insert
    python3 backfill_db.py --reports-dir /path/to/reports
"""

import argparse
import re
import sys
import uuid
from pathlib import Path

from logger import DB_PATH, get_conn, init_db

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

# Matches:  #### MEBL (Meezan Bank) — HOLD
RE_HEADER = re.compile(r"^####\s+(\w+)\s+\(([^)]+)\)\s+—\s+(\w+)", re.MULTILINE)
RE_WHEN = re.compile(r"\*\*When \(UTC\):\*\*\s+(\S+)")
RE_CONF = re.compile(r"\*\*Confidence:\*\*\s+([\d.]+)")
RE_PRICE = re.compile(r"\*\*Price at decision:\*\*\s+Rs\s+([\d,]+\.?\d*)")
RE_REASONING_START = re.compile(r"^\*\*Reasoning\*\*\s*$", re.MULTILINE)


def _parse_block(block: str, fallback_date: str):
    """Extract fields from one ticker block. Returns dict or None."""
    m = RE_HEADER.search(block)
    if not m:
        return None
    ticker, _company, action = m.group(1), m.group(2), m.group(3).upper()

    when_m = RE_WHEN.search(block)
    timestamp = when_m.group(1) if when_m else fallback_date

    conf_m = RE_CONF.search(block)
    confidence = float(conf_m.group(1)) if conf_m else 0.5

    price_m = RE_PRICE.search(block)
    price = float(price_m.group(1).replace(",", "")) if price_m else 0.0

    # Extract reasoning paragraph (text after **Reasoning** until next blank line or ##)
    reasoning = ""
    r_match = RE_REASONING_START.search(block)
    if r_match:
        after = block[r_match.end():].lstrip("\n")
        # take lines until we hit a blank line or a new section marker
        lines = []
        for line in after.splitlines():
            if line.startswith("#") or line.startswith("**Signals"):
                break
            if line.strip() == "" and lines:
                break
            lines.append(line)
        reasoning = " ".join(l.strip() for l in lines if l.strip())

    return {
        "ticker": ticker,
        "action": action,
        "timestamp": timestamp,
        "confidence": confidence,
        "price_at_decision": price,
        "reasoning": reasoning,
    }


def parse_report(path: Path):
    """Return list of decision dicts parsed from one report file."""
    text = path.read_text(encoding="utf-8", errors="replace")

    # Derive a fallback date from the filename  psx-report-20260503-142550.md
    m = re.search(r"(\d{8})-(\d{6})", path.name)
    if m:
        d, t = m.group(1), m.group(2)
        fallback = f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}"
    else:
        fallback = "2026-01-01T00:00:00"

    # Split into per-ticker blocks on #### headings
    parts = re.split(r"(?=^####\s+\w+\s+\()", text, flags=re.MULTILINE)
    decisions = []
    for part in parts:
        row = _parse_block(part, fallback)
        if row:
            decisions.append(row)
    return decisions


# ---------------------------------------------------------------------------
# DB insertion
# ---------------------------------------------------------------------------

def existing_keys(conn):
    """Return set of (ticker, timestamp) already in DB to avoid duplicates."""
    rows = conn.execute("SELECT ticker, timestamp FROM decisions").fetchall()
    return {(r[0], r[1]) for r in rows}


def insert(conn, row):
    conn.execute(
        """INSERT INTO decisions
               (id, ticker, timestamp, decision, confidence, signals, reasoning, price_at_decision)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            row["ticker"],
            row["timestamp"],
            row["action"],
            row["confidence"],
            "{}",
            row["reasoning"],
            row["price_at_decision"],
        ),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reports-dir", default="reports")
    args = ap.parse_args()

    reports_dir = Path(args.reports_dir)
    if not reports_dir.exists():
        print(f"[!] reports dir not found: {reports_dir}")
        sys.exit(1)

    # Collect all psx-report-*.md files, sorted oldest first
    files = sorted(reports_dir.glob("psx-report-*.md"))
    if not files:
        print("[!] No psx-report-*.md files found.")
        sys.exit(1)

    print(f"Found {len(files)} report files in {reports_dir}/")

    init_db()
    conn = get_conn()
    seen = existing_keys(conn)
    inserted = skipped = 0

    for f in files:
        decisions = parse_report(f)
        for row in decisions:
            key = (row["ticker"], row["timestamp"])
            if key in seen:
                skipped += 1
                continue
            if args.dry_run:
                print(f"  [dry] {row['timestamp'][:10]}  {row['ticker']:8s}  {row['action']:5s}  "
                      f"conf={row['confidence']}  price={row['price_at_decision']}")
            else:
                insert(conn, row)
                seen.add(key)
            inserted += 1

    if not args.dry_run:
        conn.commit()
        print(f"\nInserted {inserted} decisions, skipped {skipped} duplicates.")
        total = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        print(f"db.sqlite now has {total} decisions total.")

        # Refresh decisions.json
        from logger import export_decisions_json
        export_decisions_json()
    else:
        print(f"\n[dry-run] Would insert {inserted} decisions, skip {skipped} duplicates.")

    conn.close()


if __name__ == "__main__":
    main()
