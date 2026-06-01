"""JS Global InvestPro mobile API client.

Uses the vt.jsglobalonline.com/pero/ Java servlet API (reverse-engineered
from the Android app). Falls back to web portal session cookies if a
pre-seeded session is provided in profile.yaml.

Usage:
    from tools.broker import JSGlobalClient
    client = JSGlobalClient(profile)
    client.login()
    result = client.place_order("BUY", "WTL", shares=100, price=1.29)
"""

import uuid
import requests


MOBILE_BASE = "https://vt.jsglobalonline.com/pero"


class BrokerError(Exception):
    pass


class JSGlobalClient:
    def __init__(self, profile: dict):
        cfg = profile.get("broker") or {}
        self.username = str(cfg.get("username") or "")
        self.password = str(cfg.get("password") or "")
        self.pin = str(cfg.get("pin") or "")
        self.account = str(cfg.get("account") or self.username)
        self._session_id = None   # `identifier` from LoginServlet response
        self._logged_in = False
        self._http = requests.Session()
        self._http.headers.update({
            "User-Agent": "Dalvik/2.1.0 (Linux; Android 13; Pixel 6)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        })
        # Device ID — stable per installation; use account as seed
        self._device_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, self.username))

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def login(self) -> None:
        """Authenticate via LoginServlet and store the session identifier."""
        params = {
            "userid": self.username,
            "password": self.password,
            "deviceID": self._device_id,
            "isReLogin": "N",
            "FromActivity": "LoginActivity",
        }
        resp = self._http.get(
            MOBILE_BASE + "/LoginServlet",
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:
            raise BrokerError(f"LoginServlet non-JSON response: {resp.text[:300]}")

        identifier = data.get("identifier")
        if identifier is None or int(identifier) < 0:
            msg = data.get("customErrorMessage") or data.get("message") or str(data)
            raise BrokerError(f"Login failed: {msg}")

        self._session_id = str(identifier)
        self._logged_in = True

    def relogin(self) -> None:
        """Refresh the session via ReloginServlet."""
        resp = self._http.get(
            MOBILE_BASE + "/ReloginServlet",
            params={"userid": self.username, "SESSION_ID": self._session_id},
            timeout=15,
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(
        self,
        side: str,
        ticker: str,
        shares: int,
        price: float,
        market: str = "REG",
        order_type: str = "Limit",
    ) -> dict:
        """Place a buy or sell order. Returns the server's JSON response."""
        if not self._logged_in:
            self.login()

        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise BrokerError(f"Invalid side: {side!r}. Must be BUY or SELL.")
        if shares <= 0:
            raise BrokerError(f"shares must be > 0, got {shares}")
        if price <= 0:
            raise BrokerError(f"price must be > 0, got {price}")

        params = {
            "userid": self.username,
            "SESSION_ID": self._session_id,
            "acc": self.account,
            "symbol": ticker,
            "market": market,
            "qty": str(shares),
            "price": f"{price:.2f}",
            "orderType": order_type,
            "buySell": side,
            "pin": self.pin,
        }

        resp = self._http.get(
            MOBILE_BASE + "/order",
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text.strip()}

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by order ID."""
        if not self._logged_in:
            self.login()
        resp = self._http.get(
            MOBILE_BASE + "/cancelOrder",
            params={
                "userid": self.username,
                "SESSION_ID": self._session_id,
                "acc": self.account,
                "orderID": order_id,
            },
            timeout=15,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text.strip()}

    def get_logs(self, page: int = 1, page_size: int = 20) -> dict:
        """Fetch trade log entries."""
        if not self._logged_in:
            self.login()
        resp = self._http.get(
            MOBILE_BASE + "/LogsServletAndroid",
            params={
                "userid": self.username,
                "SESSION_ID": self._session_id,
                "acc": self.account,
                "logname": "OrderLog",
                "pageNo": str(page),
                "recordSize": str(page_size),
            },
            timeout=15,
        )
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text.strip()}

    def ping(self) -> bool:
        """Keepalive ping. Returns True if session is still alive."""
        try:
            resp = self._http.get(
                MOBILE_BASE + "/pingPong",
                params={"userid": self.username, "SESSION_ID": self._session_id},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def logout(self) -> None:
        self._logged_in = False
        self._session_id = None
