"""Build a live portfolio snapshot from profile + price data."""


def build_portfolio(profile, price_data_by_ticker):
    """
    profile: parsed profile.yaml dict
    price_data_by_ticker: {ticker: {"current_price": float, ...}}

    Returns a dict with positions, totals, weights, breaches.
    """
    if not profile:
        return None

    holdings = profile.get("holdings") or []
    capital = profile.get("capital") or {}
    risk = profile.get("risk") or {}

    cash = float(capital.get("investable_cash_pkr") or 0)

    positions = []
    total_cost = 0.0
    total_value = 0.0

    for h in holdings:
        ticker = h.get("ticker")
        shares = float(h.get("shares") or 0)
        avg_cost = float(h.get("avg_cost_pkr") or 0)
        if shares <= 0:
            continue
        cost_basis = shares * avg_cost

        price_info = price_data_by_ticker.get(ticker) or {}
        current_price = price_info.get("current_price")
        if current_price is None:
            current_price = avg_cost
            price_unknown = True
        else:
            price_unknown = False

        market_value = shares * current_price
        pnl_pkr = market_value - cost_basis
        pnl_pct = ((current_price - avg_cost) / avg_cost * 100) if avg_cost else 0.0

        positions.append(
            {
                "ticker": ticker,
                "shares": shares,
                "avg_cost_pkr": round(avg_cost, 4),
                "current_price_pkr": round(current_price, 4),
                "cost_basis_pkr": round(cost_basis, 2),
                "market_value_pkr": round(market_value, 2),
                "pnl_pkr": round(pnl_pkr, 2),
                "pnl_pct": round(pnl_pct, 2),
                "price_unknown": price_unknown,
            }
        )
        total_cost += cost_basis
        total_value += market_value

    total_equity = total_value + cash

    for p in positions:
        p["weight_pct"] = (
            round((p["market_value_pkr"] / total_equity) * 100, 2)
            if total_equity
            else 0.0
        )

    cash_pct = round((cash / total_equity) * 100, 2) if total_equity else 100.0

    max_single = float(risk.get("max_single_position_pct") or 100)
    min_cash = float(risk.get("min_cash_reserve_pct") or 0)

    breaches = []
    for p in positions:
        if p["weight_pct"] > max_single:
            breaches.append(
                f"{p['ticker']} weight {p['weight_pct']:.1f}% exceeds cap {max_single:.0f}%"
            )
    if cash_pct < min_cash and total_equity > 0:
        breaches.append(
            f"cash reserve {cash_pct:.1f}% below minimum {min_cash:.0f}%"
        )

    return {
        "positions": positions,
        "total_cost_pkr": round(total_cost, 2),
        "total_market_value_pkr": round(total_value, 2),
        "total_pnl_pkr": round(total_value - total_cost, 2),
        "total_pnl_pct": (
            round(((total_value - total_cost) / total_cost) * 100, 2)
            if total_cost
            else 0.0
        ),
        "cash_pkr": round(cash, 2),
        "total_equity_pkr": round(total_equity, 2),
        "cash_pct": cash_pct,
        "breaches": breaches,
    }


def position_for(portfolio, ticker):
    if not portfolio:
        return None
    for p in portfolio.get("positions", []):
        if p["ticker"] == ticker:
            return p
    return None


def compute_buy_allocation(
    portfolio,
    profile,
    action,
    current_price,
    held,
    sector_mv_pkr=0.0,
    volume_signal=None,
):
    """Rupee + share count for one ENTER/ADD from live cash and risk caps.

    Uses ``portfolio.cash_pkr`` (from ``investable_cash_pkr``), enforces
    ``min_cash_reserve_pct`` as a minimum cash balance vs total equity,
    ``max_single_position_pct`` (headroom vs current holding for ADD),
    and ``max_sector_exposure_pct`` vs ``sector_mv_pkr`` (current sector MV).

    Returns a dict with ``shares``, ``size_pkr``, and audit fields for reports.
    """
    base = {
        "shares": 0,
        "size_pkr": 0.0,
        "deployable_cash_pkr": 0.0,
        "min_cash_floor_pkr": 0.0,
        "max_spend_pkr": 0.0,
        "single_position_room_pkr": 0.0,
        "sector_room_pkr": 0.0,
        "capped_by": [],
    }
    if action not in ("ENTER", "ADD") or not portfolio or not profile:
        return base
    price = float(current_price or 0)
    if price <= 0:
        return {**base, "capped_by": ["invalid or missing price"]}

    risk = profile.get("risk") or {}
    max_single_pct = float(risk.get("max_single_position_pct") or 100)
    max_sector_pct = float(risk.get("max_sector_exposure_pct") or 100)
    min_cash_pct = float(risk.get("min_cash_reserve_pct") or 0)

    cash = float(portfolio["cash_pkr"])
    total_equity = float(portfolio["total_equity_pkr"])
    if total_equity <= 0:
        return {**base, "capped_by": ["zero equity"]}

    min_cash_floor = total_equity * (min_cash_pct / 100.0)
    deployable = max(0.0, cash - min_cash_floor)

    max_position_value = total_equity * (max_single_pct / 100.0)
    if held:
        current_mv = float(held.get("market_value_pkr") or 0)
        room_single = max(0.0, max_position_value - current_mv)
    else:
        room_single = max_position_value

    sector_room = max(
        0.0,
        (max_sector_pct / 100.0) * total_equity - float(sector_mv_pkr or 0),
    )

    max_spend = min(deployable, room_single, sector_room)
    capped_by = []
    if max_spend == deployable:
        capped_by.append("deployable cash (after min cash reserve)")
    if max_spend == room_single:
        capped_by.append("single-name cap")
    if max_spend == sector_room:
        capped_by.append("sector cap")
    if not capped_by:
        capped_by.append("no capacity")

    if max_spend <= 0:
        reasons = []
        if deployable <= 0:
            reasons.append("no cash after min reserve")
        if room_single <= 0:
            reasons.append("at or above single-position limit")
        if sector_room <= 0:
            reasons.append("at or above sector exposure limit")
        return {
            **base,
            "min_cash_floor_pkr": round(min_cash_floor, 2),
            "deployable_cash_pkr": round(deployable, 2),
            "single_position_room_pkr": round(room_single, 2),
            "sector_room_pkr": round(sector_room, 2),
            "capped_by": reasons or ["no capacity"],
        }

    if volume_signal == "low":
        max_spend = max_spend * 0.5
        capped_by = list(capped_by) + ["low-volume cap (50%)"]

    shares = int(max_spend // price)
    size_pkr = round(shares * price, 2)
    if shares == 0 and max_spend > 0:
        capped_by = list(capped_by) + [
            f"max spend Rs {max_spend:,.0f} below one share at Rs {price:,.2f}"
        ]
    return {
        "shares": shares,
        "size_pkr": size_pkr,
        "deployable_cash_pkr": round(deployable, 2),
        "min_cash_floor_pkr": round(min_cash_floor, 2),
        "max_spend_pkr": round(max_spend, 2),
        "single_position_room_pkr": round(room_single, 2),
        "sector_room_pkr": round(sector_room, 2),
        "capped_by": capped_by,
    }
