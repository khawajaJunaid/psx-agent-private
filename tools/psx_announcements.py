"""Scrape official corporate announcements from PSX Data Portal.

Source: https://dps.psx.com.pk/company/<TICKER>

The page renders three tab panels server-side (no JS required):
  - Financial Results
  - Board Meetings
  - Others

Each panel contains a <table class="tbl"> with rows of (date, title, document).
"""

from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    )
}
BASE_URL = "https://dps.psx.com.pk/company/{ticker}"
PDF_BASE = "https://dps.psx.com.pk"
DATE_FMT = "%b %d, %Y"


def _parse_date(text):
    try:
        return datetime.strptime(text.strip(), DATE_FMT).date()
    except (ValueError, AttributeError):
        return None


def _extract_panel(panel, category):
    items = []
    for row in panel.select("table.tbl tbody.tbl__body tr"):
        tds = row.find_all("td", recursive=False)
        if len(tds) < 3:
            continue
        date_text = tds[0].get_text(strip=True)
        title = tds[1].get_text(" ", strip=True)
        pdf_link = tds[2].find("a", href=lambda h: h and h.endswith(".pdf"))
        pdf_url = f"{PDF_BASE}{pdf_link['href']}" if pdf_link else None
        items.append({
            "date": date_text,
            "date_obj": _parse_date(date_text),
            "category": category,
            "title": title,
            "pdf_url": pdf_url,
        })
    return items


def _enrich_with_pdf_content(items, max_pdfs, max_chars):
    """Mutates items in place, adding `content`/`content_chars`/`content_error` fields.

    Only the most recent `max_pdfs` filings are enriched (to keep per-run cost bounded);
    everything else just keeps title + pdf_url. PDF extraction is JVM-backed and cached
    per document ID, so repeated runs are nearly free.
    """
    if not items or max_pdfs <= 0:
        return
    try:
        from .pdf_extract import extract_many, doc_id_from_url
    except ImportError:
        return

    targets = [it for it in items if it.get("pdf_url")][:max_pdfs]
    specs = [{"pdf_url": it["pdf_url"]} for it in targets]
    extracted = extract_many(specs, max_chars=max_chars)

    for it in targets:
        doc_id = doc_id_from_url(it["pdf_url"])
        info = extracted.get(doc_id) if doc_id else None
        if not info:
            continue
        it["content"] = info.get("text") or ""
        it["content_chars"] = info.get("char_count", 0)
        it["content_method"] = info.get("method", "text-layer")
        if info.get("error"):
            it["content_error"] = info["error"]


def get_announcements(ticker, days=30, max_results=10, extract_pdfs=3, max_pdf_chars=2000):
    """Return recent corporate filings for a PSX ticker.

    Args:
        ticker: PSX symbol (e.g. "MEBL"). Case-insensitive.
        days: only include filings from the last N days. Use None for all.
        max_results: cap on total filings returned (after date filter, sorted newest-first).
        extract_pdfs: number of most-recent filings to download + parse for full text.
                      Set to 0 to skip PDF extraction entirely.
        max_pdf_chars: per-filing markdown truncation limit.

    Returns:
        {
            "ticker": "MEBL",
            "announcements": [
                {"date": "Apr 29, 2026", "category": "Financial Results",
                 "title": "...", "pdf_url": "https://...",
                 "content": "...extracted markdown...",  # if extracted
                 "content_chars": 1234},
                ...
            ],
            "count": <int>,
            "source": "PSX DPS",
        }
        or {"ticker": ..., "announcements": [], "count": 0, "error": "..."} on failure.
    """
    ticker = ticker.upper().strip()
    try:
        resp = requests.get(BASE_URL.format(ticker=ticker), headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        items = []
        for panel in soup.select("#announcementsTab .tabs__panel"):
            category = panel.get("data-name", "Other")
            items.extend(_extract_panel(panel, category))

        if days is not None:
            cutoff = datetime.utcnow().date() - timedelta(days=days)
            items = [it for it in items if it["date_obj"] and it["date_obj"] >= cutoff]

        items.sort(key=lambda it: it["date_obj"] or datetime.min.date(), reverse=True)
        items = items[:max_results]

        clean = [
            {"date": it["date"], "category": it["category"],
             "title": it["title"], "pdf_url": it["pdf_url"]}
            for it in items
        ]

        _enrich_with_pdf_content(clean, max_pdfs=extract_pdfs, max_chars=max_pdf_chars)

        return {
            "ticker": ticker,
            "announcements": clean,
            "count": len(clean),
            "source": "PSX DPS",
        }
    except Exception as e:
        return {
            "ticker": ticker,
            "announcements": [],
            "count": 0,
            "error": str(e),
            "source": "PSX DPS",
        }


if __name__ == "__main__":
    import json
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "MEBL"
    print(json.dumps(get_announcements(sym, days=60), indent=2))
