"""Load and validate the user's personal investing profile."""
from pathlib import Path
import yaml

PROFILE_PATH = Path(__file__).parent.parent / "profile.yaml"


def load_profile():
    """Return the parsed profile dict, or None if profile.yaml doesn't exist."""
    if not PROFILE_PATH.exists():
        return None
    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def holdings_tickers(profile):
    if not profile:
        return []
    holdings = profile.get("holdings") or []
    return [h["ticker"] for h in holdings if h.get("ticker") and (h.get("shares") or 0) > 0]


def get_holding(profile, ticker):
    """Return holding dict for a ticker, or None if not held."""
    if not profile:
        return None
    for h in profile.get("holdings") or []:
        if h.get("ticker") == ticker and (h.get("shares") or 0) > 0:
            return h
    return None


def apply_price_overrides(profile, prices_by_ticker):
    """Overlay ``current_price`` from profile so quotes can match your broker app.

    - ``price_overrides``: map of ticker -> PKR last price (any watchlist name).
    - ``holdings[].broker_last_price_pkr``: per-line override (applied after map;
      wins for that ticker).

    Mutates dicts returned by ``tools.price.get_price_data`` in place.
    """
    if not profile or not prices_by_ticker:
        return
    for t, px in (profile.get("price_overrides") or {}).items():
        if not t or px is None:
            continue
        p = prices_by_ticker.get(t)
        if not p or p.get("error"):
            continue
        p["current_price"] = round(float(px), 2)
        p["price_source"] = "profile.price_overrides"
    for h in profile.get("holdings") or []:
        t = h.get("ticker")
        br = h.get("broker_last_price_pkr")
        if not t or br is None:
            continue
        p = prices_by_ticker.get(t)
        if not p or p.get("error"):
            continue
        p["current_price"] = round(float(br), 2)
        p["price_source"] = "profile.holdings.broker_last_price_pkr"
