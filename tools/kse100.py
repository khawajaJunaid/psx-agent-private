"""
KSE100 index snapshot.

Yahoo's ``^KSE`` is unreliable for PSX; this module scrapes the official PSX
data portal (`dps.psx.com.pk`), which is the same source `tools/psx_quote.py`
uses for equity quotes. Falls back to yfinance only if PSX is unreachable.

Public:
    fetch_kse100_snapshot()  -> dict with last/daily_pct/etc., or {"error": "..."}
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PSX_HOME = "https://www.psx.com.pk/"
PSX_DPS_INDICES = "https://dps.psx.com.pk/indices"
PSX_DPS_HOME = "https://dps.psx.com.pk/"


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("%", "")
    if not s or s in ("—", "-", "N/A", "NA"):
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _try_dps_indices() -> Optional[Dict[str, Any]]:
    """Scrape https://dps.psx.com.pk/indices for the KSE100 row.

    The page renders ALL indices in one flat list of cells, like:
        KSE100 | 165,678.08 | 2683.91 | (1.65%) | KSE100PR | 50,835.55 | ...

    Per index the four cells are: name | last | change | (pct%).
    Sign of `change` carries direction; `pct` is bracketed and may be negative.
    """
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        sess.get(PSX_DPS_HOME, timeout=20)
        r = sess.get(PSX_DPS_INDICES, timeout=25)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

    cells: list[str] = []
    for row in soup.find_all(["tr", "div"]):
        text = row.get_text(" | ", strip=True)
        if not text:
            continue
        if "KSE100" not in text.upper():
            continue
        cells = [p.strip() for p in text.split("|") if p.strip()]
        if cells:
            break

    if not cells:
        return None

    idx = -1
    for i, c in enumerate(cells):
        if c.upper().replace("-", "").replace(" ", "") == "KSE100":
            idx = i
            break
    if idx < 0 or idx + 3 >= len(cells):
        return None

    last = _to_float(cells[idx + 1])
    change = _to_float(cells[idx + 2])
    pct_cell = cells[idx + 3]
    change_pct = _to_float(pct_cell)
    if change_pct is not None and "(" in pct_cell and "-" not in pct_cell:
        pass

    if last is None or last < 1000:
        return None
    return {
        "source": "psx_dps",
        "last": last,
        "daily_change": change,
        "daily_pct": change_pct,
    }


def _try_psx_home() -> Optional[Dict[str, Any]]:
    """Scrape psx.com.pk homepage; the KSE100 ticker is usually on top."""
    try:
        r = requests.get(PSX_HOME, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

    text = soup.get_text(" ", strip=True)
    m = re.search(
        r"KSE\s*100[^0-9\-]{0,30}([\d,]+(?:\.\d+)?)\s*([+\-]?[\d,]+(?:\.\d+)?)?\s*\(?\s*([+\-]?[\d.]+)\s*%?\s*\)?",
        text,
        re.IGNORECASE,
    )
    if not m:
        return None
    last = _to_float(m.group(1))
    change = _to_float(m.group(2))
    change_pct = _to_float(m.group(3))
    if last is None or last < 1000:
        return None

    return {
        "source": "psx_home",
        "last": last,
        "daily_change": change,
        "daily_pct": change_pct,
    }


def _try_yfinance() -> Optional[Dict[str, Any]]:
    """Fallback: try a few yfinance symbol variants for KSE100."""
    try:
        import yfinance as yf
    except Exception:
        return None
    for sym in ("^KSE", "^KSE100", "KSE100.KA", "^KSE.KA"):
        try:
            data = yf.download(sym, period="10d", interval="1d", progress=False)
        except Exception:
            continue
        if data is None or data.empty:
            continue
        close = data["Close"].dropna()
        if hasattr(close, "iloc") is False or len(close) < 2:
            continue
        try:
            last = float(close.iloc[-1])
            prev = float(close.iloc[-2])
            five_back = float(close.iloc[-6]) if len(close) >= 6 else float(close.iloc[0])
            return {
                "source": f"yfinance:{sym}",
                "last": round(last, 2),
                "daily_change": round(last - prev, 2),
                "daily_pct": round((last - prev) / prev * 100, 2),
                "five_day_pct": round((last - five_back) / five_back * 100, 2),
            }
        except Exception:
            continue
    return None


def fetch_kse100_snapshot() -> Dict[str, Any]:
    """Return KSE100 snapshot dict or {"error": "..."}.

    Successful keys:
        source, last, daily_change, daily_pct, [five_day_pct]
    """
    snap = _try_dps_indices()
    if snap and snap.get("last") is not None:
        return snap
    snap = _try_psx_home()
    if snap and snap.get("last") is not None:
        return snap
    snap = _try_yfinance()
    if snap and snap.get("last") is not None:
        return snap
    return {"error": "all KSE100 sources failed (psx_dps, psx_home, yfinance)"}
