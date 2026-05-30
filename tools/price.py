import yfinance as yf

from tools.psx_quote import fetch_dps_equity_quote


def _scalar(value):
    """Coerce a pandas scalar / 1-element Series / numpy 0-d array to a Python float."""
    try:
        return float(value.item())
    except (AttributeError, ValueError):
        return float(value)


def get_price_data(ticker):
    """Price snapshot: **PSX DPS** spot (bid/ask mid) for ``current_price`` when available;
    Yahoo (``.KA``) for 30d trend, RSI-14, and volume signal.
    """
    yf_ticker = f"{ticker}.KA"
    dps = fetch_dps_equity_quote(ticker)
    try:
        data = yf.download(
            yf_ticker, period="1y", interval="1d", progress=False, auto_adjust=True
        )
        if data.empty:
            if dps and dps.get("current_price"):
                return {
                    "ticker": ticker,
                    "current_price": float(dps["current_price"]),
                    "trend_30d_pct": 0.0,
                    "rsi_14": 50.0,
                    "rsi_signal": "neutral",
                    "volume_signal": "normal",
                    "price_source": "psx_dps",
                    "yahoo_bar_close": None,
                    **{k: v for k, v in dps.items() if k != "current_price"},
                }
            return {"error": f"No data found for {yf_ticker}", "ticker": ticker}
        close = data["Close"].dropna()
        bar_close = _scalar(close.iloc[-1])
        if dps and dps.get("current_price"):
            current_price = float(dps["current_price"])
            price_source = "psx_dps"
        else:
            current_price = bar_close
            price_source = "yahoo_daily_bar"
        price_30d_ago = _scalar(close.iloc[-30]) if len(close) >= 30 else _scalar(close.iloc[0])
        trend_pct = ((current_price - price_30d_ago) / price_30d_ago) * 100
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + _scalar(rs.iloc[-1])))
        vol_today = _scalar(data["Volume"].iloc[-1])
        vol_avg = _scalar(data["Volume"].tail(20).mean())
        if vol_today > vol_avg * 1.5:
            volume_signal = "high"
        elif vol_today > vol_avg * 0.5:
            volume_signal = "normal"
        else:
            volume_signal = "low"
        high_52w = _scalar(close.tail(252).max()) if len(close) >= 20 else None
        low_52w = _scalar(close.tail(252).min()) if len(close) >= 20 else None
        if high_52w and low_52w and (high_52w - low_52w) > 0:
            range_position_pct = round(
                ((current_price - low_52w) / (high_52w - low_52w)) * 100, 1
            )
        else:
            range_position_pct = None
        out = {
            "ticker": ticker,
            "current_price": round(current_price, 2),
            "trend_30d_pct": round(trend_pct, 2),
            "rsi_14": round(rsi, 2),
            "rsi_signal": "oversold" if rsi < 35 else "overbought" if rsi > 70 else "neutral",
            "volume_signal": volume_signal,
            "price_source": price_source,
            "yahoo_bar_close": round(bar_close, 2),
            "high_52w": round(high_52w, 2) if high_52w else None,
            "low_52w": round(low_52w, 2) if low_52w else None,
            "range_position_pct": range_position_pct,
        }
        if dps:
            for k, v in dps.items():
                if k != "current_price" and v is not None:
                    out[k] = v
        return out
    except Exception as e:
        if dps and dps.get("current_price"):
            return {
                "ticker": ticker,
                "current_price": round(float(dps["current_price"]), 2),
                "trend_30d_pct": 0.0,
                "rsi_14": 50.0,
                "rsi_signal": "neutral",
                "volume_signal": "normal",
                "price_source": "psx_dps",
                "yahoo_bar_close": None,
                "yahoo_error": str(e),
                **{k: v for k, v in dps.items() if k != "current_price"},
            }
        return {"error": str(e), "ticker": ticker}
