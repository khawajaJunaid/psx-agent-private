"""Multi-source PSX-relevant news headlines.

Dawn HTML search and **Business Recorder** (`brecorder.com`) often return **403**
to plain ``requests``. **Business Recorder** stories still appear when **Google
News RSS** indexes them.

**Optional — Crawl4AI** ([unclecode/crawl4ai](https://github.com/unclecode/crawl4ai)):
a headless Chromium via Playwright sometimes passes those blocks. It is **not**
installed by default (heavy: Playwright browsers ~300MB).

  pip install crawl4ai
  crawl4ai-setup   # or: playwright install

  export PSX_USE_CRAWL4AI=1
  # Optional: always try browser for Dawn + BR (even when RSS is full):
  # export PSX_CRAWL4AI_ALWAYS=1
  # Optional: trigger browser only if fewer than N headlines (default 4):
  # export PSX_CRAWL4AI_MIN_HEADLINES=4

This module aggregates:

1. Google News RSS (company + optional ticker + Pakistan/PSX context)
2. Keyword-filtered items from Pakistani business RSS feeds (direct):
   - Express Tribune — business
   - Profit (Pakistan Today)
   - ProPakistani
3. Optional ``requests`` Dawn HTML (usually 403)
4. Optional Crawl4AI: Dawn search + Business Recorder search (env-gated)

Headlines are de-duplicated (case-insensitive). ``sources_used`` lists which
paths contributed rows so callers can debug empty results.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Iterable, List, Optional, Set, Tuple
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-PK,en;q=0.9,en-US;q=0.8",
}

# RSS feeds scanned when Google alone is thin; titles must match company/ticker keywords.
BUSINESS_RSS_FEEDS = (
    ("tribune_business_rss", "https://tribune.com.pk/business/feed/"),
    ("profit_rss", "https://profit.pakistantoday.com.pk/feed/"),
    ("propakistani_rss", "https://propakistani.pk/feed/"),
)

NAME_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "ltd",
        "limited",
        "company",
        "companies",
        "corporation",
        "plc",
        "pakistan",
        "pakistani",
        "state",
        "public",
        "private",
        "of",
        "to",
        "in",
    }
)


def _clean_title(text: str) -> str:
    t = unescape((text or "").strip())
    t = re.sub(r"\s+", " ", t)
    # Strip common Google News suffixes like " - Dawn"
    return t.strip()


def _norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _keywords(company_name: str, ticker: Optional[str]) -> Set[str]:
    kws: Set[str] = set()
    if ticker:
        t = ticker.strip().upper()
        if t:
            kws.add(t)
    for w in re.split(r"[\s,&/'().-]+", company_name or ""):
        w = w.strip()
        if len(w) < 3:
            continue
        low = w.lower()
        if low in NAME_STOPWORDS:
            continue
        kws.add(w)
    return kws


def _title_matches(title: str, keywords: Set[str]) -> bool:
    tl = title.lower()
    for kw in keywords:
        k = kw.lower()
        if len(k) <= 4:
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(k)}(?![A-Za-z0-9])", tl):
                return True
        else:
            if k in tl:
                return True
    return False


def _parse_rss_pubdate(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _parse_rss_items(xml_bytes: bytes) -> List[Tuple[str, Optional[datetime]]]:
    """Return ``[(title, pub_dt_utc_or_None), ...]`` from RSS bytes."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    out: List[Tuple[str, Optional[datetime]]] = []
    for item in root.iter():
        if not (item.tag.endswith("item") or item.tag == "item"):
            continue
        title = None
        pub = None
        for child in item:
            tag = child.tag.split("}")[-1].lower()
            if tag == "title" and child.text and title is None:
                title = _clean_title(child.text)
            elif tag in ("pubdate", "date", "published", "updated") and child.text and pub is None:
                pub = _parse_rss_pubdate(child.text)
        if title:
            out.append((title, pub))
    return out


def _parse_rss_titles(xml_bytes: bytes) -> List[str]:
    return [t for t, _ in _parse_rss_items(xml_bytes)]


def _filter_recent(
    items: List[Tuple[str, Optional[datetime]]], max_age_hours: Optional[float]
) -> List[Tuple[str, Optional[datetime]]]:
    if not max_age_hours:
        return items
    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_hours * 3600)
    out: List[Tuple[str, Optional[datetime]]] = []
    for title, dt in items:
        if dt is None:
            continue
        if dt.timestamp() >= cutoff:
            out.append((title, dt))
    return out


def _google_news_headlines(
    session: requests.Session,
    company_name: str,
    ticker: Optional[str],
    max_results: int,
    max_age_hours: Optional[float] = None,
) -> tuple[List[Tuple[str, Optional[datetime]]], Optional[str]]:
    parts = [company_name, "Pakistan", "PSX"]
    if ticker:
        parts.insert(1, ticker)
    q = quote_plus(" ".join(parts))
    url = f"https://news.google.com/rss/search?q={q}&hl=en-PK&gl=PK&ceid=PK:en"
    try:
        r = session.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return [], str(e)
    items = _parse_rss_items(r.content)
    items = _filter_recent(items, max_age_hours)
    return items[: max_results], None


def _parse_dawn_search_html(html: str, max_results: int) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    headlines: List[str] = []
    for article in soup.select("article.story")[:max_results]:
        title_el = article.select_one("h2.story__title, h3.story__title")
        if title_el:
            headlines.append(title_el.get_text(strip=True))
    return headlines


def _parse_brecorder_search_html(html: str, max_results: int) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    seen: Set[str] = set()
    out: List[str] = []
    selectors = (
        "article h2 a",
        "h2.entry-title a",
        ".jeg_post_title a",
        "h3.entry-title a",
        ".post-title a",
    )
    for sel in selectors:
        for a in soup.select(sel):
            t = a.get_text(strip=True)
            if not t or len(t) < 12:
                continue
            nk = _norm_key(t)
            if nk in seen:
                continue
            seen.add(nk)
            out.append(t)
            if len(out) >= max_results:
                return out
    return out


def _dawn_search_headlines(
    session: requests.Session, company_name: str, max_results: int
) -> tuple[List[str], Optional[str]]:
    """Last-resort HTML scrape; often 403 — kept for completeness."""
    q = quote_plus(company_name)
    url = f"https://www.dawn.com/search?q={q}&categories=business"
    h = {
        **HEADERS,
        "Referer": "https://www.dawn.com/",
    }
    try:
        r = session.get(url, headers=h, timeout=12)
        r.raise_for_status()
    except Exception as e:
        return [], str(e)
    return _parse_dawn_search_html(r.text, max_results), None


def _env_truthy(name: str) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _crawl4ai_fetch_html(url: str) -> tuple[Optional[str], Optional[str]]:
    """Return (html, error). Requires ``crawl4ai`` + Playwright browsers."""
    import asyncio

    async def _run():
        try:
            from crawl4ai import AsyncWebCrawler
            from crawl4ai.async_configs import BrowserConfig, CrawlerRunConfig, CacheMode
        except ImportError as e:
            return None, f"crawl4ai not installed: {e}"

        browser_config = BrowserConfig(headless=True, verbose=False)
        run_config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            word_count_threshold=3,
        )
        try:
            async with AsyncWebCrawler(config=browser_config) as crawler:
                result = await crawler.arun(url=url, config=run_config)
        except Exception as e:
            return None, str(e)

        if not getattr(result, "success", False):
            return None, (
                getattr(result, "error_message", None)
                or getattr(result, "error", None)
                or "crawl unsuccessful"
            )

        html = getattr(result, "html", None) or getattr(result, "cleaned_html", None) or ""
        if not html.strip():
            return None, "empty html"
        return html, None

    try:
        return asyncio.run(_run())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(_run())
        finally:
            loop.close()


def _crawl4ai_supplement(
    company_name: str,
    ticker: Optional[str],
    seen: Set[str],
    merged: List[str],
    max_results: int,
    sources_used: List[str],
    errors: List[str],
) -> None:
    """Mutates ``merged`` / ``sources_used`` / ``errors`` in place."""
    need = max_results - len(merged)
    if need <= 0 and not _env_truthy("PSX_CRAWL4AI_ALWAYS"):
        return

    q = quote_plus(company_name)
    cap = max(need, 4) if _env_truthy("PSX_CRAWL4AI_ALWAYS") else need

    dawn_url = f"https://www.dawn.com/search?q={q}&categories=business"
    html, err = _crawl4ai_fetch_html(dawn_url)
    if err:
        errors.append(f"crawl4ai_dawn: {err}")
    elif html:
        titles = _parse_dawn_search_html(html, cap)
        if titles:
            sources_used.append("crawl4ai_dawn")
            merged.extend(_merge_unique(titles, seen, max_results))

    if len(merged) >= max_results and not _env_truthy("PSX_CRAWL4AI_ALWAYS"):
        return

    need2 = max(max_results - len(merged), 3 if _env_truthy("PSX_CRAWL4AI_ALWAYS") else 0)
    if need2 <= 0:
        return

    br_url = f"https://www.brecorder.com/?s={q}"
    html2, err2 = _crawl4ai_fetch_html(br_url)
    if err2:
        errors.append(f"crawl4ai_brecorder: {err2}")
    elif html2:
        titles2 = _parse_brecorder_search_html(html2, need2 + 4)
        if titles2:
            sources_used.append("crawl4ai_brecorder")
            merged.extend(_merge_unique(titles2, seen, max_results))


def _rss_filtered_headlines(
    session: requests.Session,
    feed_label: str,
    feed_url: str,
    keywords: Set[str],
    max_results: int,
    seen: Set[str],
    max_age_hours: Optional[float] = None,
) -> tuple[List[Tuple[str, Optional[datetime]]], Optional[str]]:
    try:
        r = session.get(feed_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        return [], str(e)
    items = _parse_rss_items(r.content)
    items = _filter_recent(items, max_age_hours)
    picked: List[Tuple[str, Optional[datetime]]] = []
    for t, dt in items:
        if len(picked) >= max_results:
            break
        if not _title_matches(t, keywords):
            continue
        nk = _norm_key(t)
        if nk in seen:
            continue
        seen.add(nk)
        picked.append((t, dt))
    return picked, None


def _merge_unique(headlines: Iterable[str], seen: Set[str], cap: int) -> List[str]:
    out: List[str] = []
    for h in headlines:
        if len(out) >= cap:
            break
        nk = _norm_key(h)
        if not nk or nk in seen:
            continue
        seen.add(nk)
        out.append(h)
    return out


def get_news_headlines(
    company_name: str,
    max_results: int = 8,
    ticker: Optional[str] = None,
    max_age_hours: Optional[float] = None,
) -> dict:
    """
    Returns:
        company, headlines, count, sources_used, errors, optional legacy "error".

    Extra keys when ``max_age_hours`` is set or pubDate is available:
        items: [{"title": str, "published_utc": iso_str | None, "age_hours": float | None}, ...]
        max_age_hours: the filter applied (for caller telemetry)

    ``max_age_hours=None`` (default) preserves the old behavior — undated
    items are kept. When set, undated items are dropped.
    """
    company_name = (company_name or "").strip()
    if not company_name:
        return {
            "company": company_name,
            "headlines": [],
            "items": [],
            "count": 0,
            "sources_used": [],
            "errors": ["empty company name"],
            "error": "empty company name",
        }

    session = requests.Session()
    seen: Set[str] = set()
    merged_items: List[Tuple[str, Optional[datetime]]] = []
    sources_used: List[str] = []
    errors: List[str] = []

    g_items, g_err = _google_news_headlines(
        session, company_name, ticker, max_results=max_results, max_age_hours=max_age_hours
    )
    if g_err:
        errors.append(f"google_rss: {g_err}")
    if g_items:
        sources_used.append("google_rss")
        for title, dt in g_items:
            if len(merged_items) >= max_results:
                break
            nk = _norm_key(title)
            if not nk or nk in seen:
                continue
            seen.add(nk)
            merged_items.append((title, dt))

    kws = _keywords(company_name, ticker)
    if len(merged_items) < max_results and kws:
        per_feed = max(3, max_results - len(merged_items))
        for label, feed_url in BUSINESS_RSS_FEEDS:
            if len(merged_items) >= max_results:
                break
            extra_items, err = _rss_filtered_headlines(
                session, label, feed_url, kws, per_feed, seen, max_age_hours=max_age_hours
            )
            if err:
                errors.append(f"{label}: {err}")
                continue
            if extra_items:
                sources_used.append(label)
                for title, dt in extra_items:
                    if len(merged_items) >= max_results:
                        break
                    nk = _norm_key(title)
                    if not nk or nk in seen:
                        continue
                    seen.add(nk)
                    merged_items.append((title, dt))

    merged: List[str] = [t for t, _ in merged_items]

    # Optional Dawn scrape if still short (usually fails with 403).
    if len(merged) < max(2, max_results // 2):
        d_titles, d_err = _dawn_search_headlines(
            session, company_name, max_results=max_results - len(merged)
        )
        if d_err:
            errors.append(f"dawn_html: {d_err}")
        elif d_titles:
            sources_used.append("dawn_html")
            merged.extend(_merge_unique(d_titles, seen, max_results))

    # Optional Crawl4AI (headless browser) — Dawn + Business Recorder HTML.
    if _env_truthy("PSX_USE_CRAWL4AI"):
        min_h = int(os.getenv("PSX_CRAWL4AI_MIN_HEADLINES", "4"))
        if _env_truthy("PSX_CRAWL4AI_ALWAYS") or len(merged) < min_h:
            _crawl4ai_supplement(
                company_name,
                ticker,
                seen,
                merged,
                max_results,
                sources_used,
                errors,
            )

    merged = merged[:max_results]
    item_titles = {t for t, _ in merged_items}
    items_out = []
    now_utc = datetime.now(timezone.utc)
    for t in merged:
        dt = next((d for tt, d in merged_items if tt == t), None)
        age_h = round((now_utc - dt).total_seconds() / 3600.0, 1) if dt else None
        items_out.append(
            {
                "title": t,
                "published_utc": dt.isoformat() if dt else None,
                "age_hours": age_h,
                "dated": t in item_titles,
            }
        )

    out = {
        "company": company_name,
        "headlines": merged,
        "items": items_out,
        "count": len(merged),
        "sources_used": sources_used,
        "errors": errors,
        "max_age_hours": max_age_hours,
    }
    if not merged and errors:
        out["error"] = "; ".join(errors[:3])
    return out


if __name__ == "__main__":
    import json
    import sys

    name = " ".join(sys.argv[1:]) or "Meezan Bank"
    print(json.dumps(get_news_headlines(name, max_results=8, ticker="MEBL"), indent=2))
