"""Spot equity quotes from PSX Data Portal company pages.

https://dps.psx.com.pk/company/<TICKER> exposes ``div.stats_label`` /
``div.stats_value`` pairs (Bid/Ask/LDCP, etc.). A session warm-up request to
the site root avoids intermittent 502 responses.

Used as the primary ``current_price`` in ``tools.price``; Yahoo Finance remains
for historical OHLC / RSI / volume signals.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_DPS_SESSION: Optional[requests.Session] = None


def reset_dps_session() -> None:
    """Close and drop the shared session (e.g. at the start of ``agent.py``)."""
    global _DPS_SESSION
    if _DPS_SESSION is not None:
        try:
            _DPS_SESSION.close()
        except Exception:
            pass
        _DPS_SESSION = None


def _ensure_dps_session() -> requests.Session:
    global _DPS_SESSION
    if _DPS_SESSION is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        s.get("https://dps.psx.com.pk/", timeout=25)
        _DPS_SESSION = s
    return _DPS_SESSION


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s or s == "—":
        return None
    m = re.search(r"(-?\d+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _stats_pairs(html: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    pairs: List[Tuple[str, str]] = []
    for it in soup.select("div.stats_item"):
        lab_el = it.select_one(".stats_label")
        val_el = it.select_one(".stats_value")
        if lab_el and val_el:
            pairs.append(
                (lab_el.get_text(strip=True), val_el.get_text(strip=True))
            )
    return pairs


def _headline_quote_price(html: str) -> Optional[float]:
    """Return the DPS headline quote (e.g. ``Rs.483.32``) when present."""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(".quote__close")
    if not el:
        return None
    return _to_float(el.get_text(" ", strip=True))


def _spot_equity_panel(pairs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """First quote block ends at the first ``LDCP`` (spot); later panels are futures."""
    for i, (lab, _) in enumerate(pairs):
        if lab == "LDCP":
            return pairs[: i + 1]
    return pairs


def fetch_dps_equity_quote(ticker: str) -> Optional[Dict[str, Any]]:
    """Return PSX DPS spot fields or ``None`` if the page is missing or unusable."""
    t = (ticker or "").strip().upper()
    if not t:
        return None
    try:
        sess = _ensure_dps_session()
        url = f"https://dps.psx.com.pk/company/{t}"
        r = sess.get(url, timeout=30)
        if r.status_code != 200:
            return None
        html = r.text
        pairs = _stats_pairs(html)
        panel = _spot_equity_panel(pairs)
        if not panel:
            return None
        m = dict(panel)
        headline = _headline_quote_price(html)
        bid = _to_float(m.get("Bid Price"))
        ask = _to_float(m.get("Ask Price"))
        ldcp = _to_float(m.get("LDCP"))
        if headline:
            current = round(headline, 2)
        elif bid and ask:
            current = round((bid + ask) / 2, 2)
        elif ask:
            current = round(ask, 2)
        elif bid:
            current = round(bid, 2)
        elif ldcp:
            current = round(ldcp, 2)
        else:
            op = _to_float(m.get("Open"))
            if not op:
                return None
            current = round(op, 2)
        vol_raw = m.get("Volume")
        vol = None
        if vol_raw:
            try:
                vol = int(str(vol_raw).replace(",", "").split()[0])
            except ValueError:
                vol = None
        return {
            "current_price": current,
            "psx_bid": bid,
            "psx_ask": ask,
            "psx_ldcp": ldcp,
            "psx_open": _to_float(m.get("Open")),
            "psx_high": _to_float(m.get("High")),
            "psx_low": _to_float(m.get("Low")),
            "psx_volume": vol,
        }
    except Exception:
        return None
