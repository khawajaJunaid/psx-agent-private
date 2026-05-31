"""JS Global web trading portal client.

Handles the randomised digit-password login and order placement.
Credentials are read from profile.yaml under the `broker` key.

Usage:
    from tools.broker import JSGlobalClient
    client = JSGlobalClient(profile)
    client.login()
    result = client.place_order("BUY", "WTL", shares=100, price=1.29)
"""

import re
import requests
from bs4 import BeautifulSoup


BASE_URL = "https://wt.jsglobalonline.com"


class BrokerError(Exception):
    pass


class JSGlobalClient:
    def __init__(self, profile: dict):
        cfg = profile.get("broker") or {}
        self.username = str(cfg.get("username") or "")
        self.password = str(cfg.get("password") or "")
        self.pin = str(cfg.get("pin") or "")
        self.account = str(cfg.get("account") or self.username)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/148.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
        })
        # Seed cookies the server expects from a prior browser visit
        self._session.cookies.set("customValues", '{"font":"Rubik","fontsize":"14"}',
                                  domain="wt.jsglobalonline.com")
        self._logged_in = False

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    def _get_enabled_digits(self) -> list[int]:
        """Fetch login page and return list of enabled Digit positions (1-indexed)."""
        resp = self._session.get(BASE_URL + "/", timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        enabled = []
        for inp in soup.find_all("input", id=re.compile(r"^Digit\d+$")):
            if inp.get("disabled") is None:
                num = int(re.search(r"\d+", inp["id"]).group())
                enabled.append(num)
        if not enabled:
            raise BrokerError("No Digit fields found on login page — portal may have changed")
        # Extract CSRF token if present
        csrf = soup.find("input", {"name": "__RequestVerificationToken"})
        self._csrf_token = csrf["value"] if csrf else None
        import sys
        print(f"[broker] CSRF token found: {self._csrf_token is not None}", file=sys.stderr)
        return enabled

    def login(self) -> None:
        """Log in to the portal, populating the session cookie."""
        enabled = self._get_enabled_digits()
        payload = {"UserName": self.username}
        if self._csrf_token:
            payload["__RequestVerificationToken"] = self._csrf_token
        for pos in enabled:
            if pos <= len(self.password):
                payload[f"Digit{pos}"] = self.password[pos - 1]
        import sys
        print(f"[broker] enabled digits: {enabled}", file=sys.stderr)
        print(f"[broker] payload (no username): { {k:v for k,v in payload.items() if k != 'UserName'} }", file=sys.stderr)

        resp = self._session.post(
            BASE_URL + "/Home/_Login",
            data=payload,
            allow_redirects=False,
            timeout=15,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-User": "?1",
                "Sec-Fetch-Dest": "document",
                "Cache-Control": "max-age=0",
            },
        )
        import sys
        print(f"[broker] login POST status: {resp.status_code}", file=sys.stderr)
        print(f"[broker] location header: {resp.headers.get('Location','')}", file=sys.stderr)
        print(f"[broker] response snippet: {resp.text[:500]}", file=sys.stderr)
        # Successful login returns a 302 redirect to /Home/Index
        if resp.status_code == 302 and "/Home/Index" in resp.headers.get("Location", ""):
            self._logged_in = True
            # Follow the redirect so the session cookies are fully established
            self._session.get(BASE_URL + "/Home/Index", timeout=15)
            return
        raise BrokerError(
            f"Login failed (HTTP {resp.status_code}). "
            "Check username/password in profile.yaml."
        )

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
        exchange: str = "KSE",
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
            "Exchange": exchange,
            "Price": f"{price:.2f}",
            "PIN": self.pin,
            "LimitPrice": "",
        }

        resp = self._session.post(
            BASE_URL + "/Home/PlaceOrder",
            data=payload,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=15,
        )
        resp.raise_for_status()
        message = resp.text.strip().strip('"')
        return message

    def logout(self) -> None:
        try:
            self._session.post(BASE_URL + "/Home/Logout", timeout=10)
        except Exception:
            pass
        self._logged_in = False
