"""JS Global web trading portal client.

Uses session cookies from profile.yaml (broker.session_cookies) to
place orders via the web portal PlaceOrder endpoint.

Usage:
    from tools.broker import JSGlobalClient
    client = JSGlobalClient(profile)
    client.login()
    result = client.place_order("BUY", "WTL", shares=100, price=1.29)
"""

import requests


BASE_URL = "https://wt.jsglobalonline.com"


class BrokerError(Exception):
    pass


class JSGlobalClient:
    def __init__(self, profile: dict):
        cfg = profile.get("broker") or {}
        self.username = str(cfg.get("username") or "")
        self.pin = str(cfg.get("pin") or "")
        self.account = str(cfg.get("account") or self.username)
        self._saved_cookies = cfg.get("session_cookies") or {}
        self._logged_in = False
        self._http = requests.Session()
        self._http.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/148.0.0.0 Safari/537.36",
        })

    def login(self) -> None:
        """Seed session cookies from profile.yaml."""
        if not self._saved_cookies:
            raise BrokerError(
                "No session_cookies in profile.yaml broker section. "
                "Log in via the web portal and copy the cookies."
            )
        for name, value in self._saved_cookies.items():
            self._http.cookies.set(name, str(value), domain="wt.jsglobalonline.com")
        self._logged_in = True

    def place_order(
        self,
        side: str,
        ticker: str,
        shares: int,
        price: float,
        market: str = "REG",
        order_type: str = "Limit",
    ) -> str:
        """Place a buy or sell order. Returns the server's confirmation message."""
        if not self._logged_in:
            self.login()

        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise BrokerError(f"Invalid side: {side!r}. Must be BUY or SELL.")
        if shares <= 0:
            raise BrokerError(f"shares must be > 0, got {shares}")
        if price <= 0:
            raise BrokerError(f"price must be > 0, got {price}")

        payload = {
            "Account": self.account,
            "BuySell": side,
            "Market": market,
            "OrderType": order_type,
            "Volume": str(shares),
            "Script": ticker,
            "Exchange": "KSE",
            "Price": f"{price:.2f}",
            "PIN": self.pin,
            "LimitPrice": "",
        }

        resp = self._http.post(
            BASE_URL + "/Home/PlaceOrder",
            data=payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.text.strip().strip('"')

    def logout(self) -> None:
        try:
            self._http.post(BASE_URL + "/Home/Logout", timeout=10)
        except Exception:
            pass
        self._logged_in = False
