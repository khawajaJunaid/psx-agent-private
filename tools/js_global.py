"""JS Global web trading — periodic summary API (authenticated session only).

There is no public API key. Session is reused in one of two ways:

1. **Saved browser session (recommended)** — run::

       pip install playwright && playwright install chromium
       python3 -m tools.js_global login

   Log in in the window, press Enter in the terminal. Cookies are stored in
   ``.cache/js_global_storage.json`` (gitignored), or override with
   ``JS_GLOBAL_STORAGE``.

2. **Manual** — set ``JS_GLOBAL_COOKIE`` to the raw ``Cookie`` header string
   from DevTools (same as curl ``-b '...'``).

Set ``JS_GLOBAL_ACCOUNT`` to your account number (digits only).

Cookies expire; run ``login`` again when API calls fail. Automated use may be
restricted by broker terms — use at your own discretion.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests

BASE = "https://wt.jsglobalonline.com"
SUMMARY_PATH = "/Home/GetPeriodicSummary"
_COOKIE_DOMAIN = "jsglobalonline.com"


def default_storage_path() -> Path:
    """Playwright ``storage_state`` JSON path (session cookies for this broker)."""
    raw = os.getenv("JS_GLOBAL_STORAGE")
    if raw:
        return Path(raw).expanduser()
    root = Path(__file__).resolve().parent.parent
    return root / ".cache" / "js_global_storage.json"


def cookie_header_from_storage(path: Path) -> str:
    """Build a ``Cookie`` header string from Playwright ``storage_state`` JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    cookies = data.get("cookies") or []
    parts: list[str] = []
    for c in cookies:
        domain = (c.get("domain") or "").lstrip(".").lower()
        if _COOKIE_DOMAIN not in domain:
            continue
        name, value = c.get("name"), c.get("value")
        if name and value is not None:
            parts.append(f"{name}={value}")
    return "; ".join(parts)


def resolve_cookie(explicit: Optional[str] = None) -> str:
    """Cookie header: explicit, ``JS_GLOBAL_COOKIE``, ``JS_GLOBAL_COOKIE_FILE``, then storage JSON."""
    c = (explicit or os.getenv("JS_GLOBAL_COOKIE") or "").strip()
    if c:
        return c
    cf = (os.getenv("JS_GLOBAL_COOKIE_FILE") or "").strip()
    if cf:
        p = Path(cf).expanduser()
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / p
        if p.is_file():
            c = p.read_text(encoding="utf-8").splitlines()[0].strip()
            if c:
                return c
    path = default_storage_path()
    if path.is_file():
        c = cookie_header_from_storage(path).strip()
        if c:
            return c
    raise ValueError(
        "No session: run `python3 -m tools.js_global login`, set JS_GLOBAL_COOKIE, "
        "or set JS_GLOBAL_COOKIE_FILE to a one-line cookie file."
    )


def interactive_login(storage_path: Optional[Path] = None) -> None:
    """Open Chromium; you log in on the broker site; save session for ``requests``."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Install Playwright first:\n  pip install playwright\n  playwright install chromium",
            file=sys.stderr,
        )
        sys.exit(1)
    path = storage_path or default_storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    print(
        "Opening browser — log in to JS Global in that window.\n"
        "When the home / trading page has fully loaded, come back here and press Enter.\n"
        f"Session file: {path}\n"
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=120_000)
        input("Press Enter after you are logged in… ")
        context.storage_state(path=str(path))
        browser.close()
    print("Saved. Fetch summary e.g.:\n  python3 -m tools.js_global --from 2026-04-02 --to 2026-05-05\n")


def get_periodic_summary(
    account: str,
    from_date: str,
    to_date: str,
    *,
    cookie: Optional[str] = None,
    timeout: float = 45.0,
) -> Any:
    """GET ``GetPeriodicSummary`` — returns parsed JSON (usually a list or dict).

    ``from_date`` / ``to_date`` are ``YYYY-MM-DD`` (same as the web UI query params).

    ``cookie``: full ``Cookie`` header value. If omitted, uses ``resolve_cookie()``.
    ``account``: if omitted, uses ``JS_GLOBAL_ACCOUNT``.
    """
    cookie = resolve_cookie(cookie)
    account = (account or os.getenv("JS_GLOBAL_ACCOUNT") or "").strip()
    if not account:
        raise ValueError("Pass account=… or set JS_GLOBAL_ACCOUNT in the environment.")

    url = f"{BASE}{SUMMARY_PATH}"
    params = {"account": account, "fromdate": from_date, "todate": to_date}
    headers = {
        "Accept": "*/*",
        "User-Agent": "Mozilla/5.0 (compatible; psx-agent-js-global/1.0)",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE}/Home/Index",
        "Cookie": cookie,
    }
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    text = (r.text or "").strip()
    if not text:
        raise RuntimeError(f"Empty body (HTTP {r.status_code}). Session may be expired.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        snippet = text[:800].replace("\n", " ")
        raise RuntimeError(
            f"Non-JSON response HTTP {r.status_code}: {snippet}"
        ) from e
    if isinstance(data, str):
        inner = data.strip().rstrip("\r\n")
        if inner.startswith("{") or inner.startswith("["):
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass
    return data


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()

    argv = sys.argv[1:]
    if argv and argv[0] == "login":
        lp = argparse.ArgumentParser(prog="python3 -m tools.js_global login")
        lp.add_argument(
            "--storage",
            type=Path,
            default=None,
            help="Where to write Playwright storage JSON (default: .cache/js_global_storage.json or JS_GLOBAL_STORAGE)",
        )
        la = lp.parse_args(argv[1:])
        interactive_login(la.storage)
        return

    parser = argparse.ArgumentParser(
        description="Call JS Global GetPeriodicSummary (session: `login` subcommand or JS_GLOBAL_COOKIE)."
    )
    parser.add_argument("--account", default=os.getenv("JS_GLOBAL_ACCOUNT"), help="Override JS_GLOBAL_ACCOUNT")
    parser.add_argument(
        "--from",
        dest="from_date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Start date (default: 30 days before --to)",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        default=None,
        metavar="YYYY-MM-DD",
        help="End date (default: today local)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Single-line JSON (easier to pipe)",
    )
    args = parser.parse_args()

    to_d = args.to_date or datetime.now().strftime("%Y-%m-%d")
    if args.from_date:
        from_d = args.from_date
    else:
        from_d = (datetime.strptime(to_d, "%Y-%m-%d") - timedelta(days=30)).strftime(
            "%Y-%m-%d"
        )

    try:
        data = get_periodic_summary(
            account=args.account or "",
            from_date=from_d,
            to_date=to_d,
        )
    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    indent = None if args.compact else 2
    print(json.dumps(data, indent=indent, ensure_ascii=False))


if __name__ == "__main__":
    main()
