"""Reconcile profile.yaml's investable_cash_pkr with what's actually in your broker.

The agent treats `investable_cash_pkr` as your total dry powder for the
strategy: cash sitting in the broker's ledger PLUS any external cash
(bank, savings) you'd realistically deploy if it recommended a BUY. If
the two drift out of sync, position-sizing and cash-reserve maths get
wrong.

Run this whenever you deposit / withdraw / spend cash, or just to sanity
check the agent's view against your broker app.

Usage
-----
  # Interactive (recommended):
  python3 sync_broker.py

  # Non-interactive:
  python3 sync_broker.py --broker-cash 3589 --external-cash 15000 [--write]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import sys
from pathlib import Path

import yaml

from tools.price import get_price_data

ROOT = Path(__file__).resolve().parent
PROFILE_PATH = ROOT / "profile.yaml"

CASH_KEY = "investable_cash_pkr"
CASH_LINE_RE = re.compile(
    rf"^(?P<indent>\s*){CASH_KEY}:\s*(?P<value>[0-9.]+)(?P<trailing>.*)$",
    re.MULTILINE,
)


def _money(value: float) -> str:
    return f"Rs {value:,.0f}"


def _pct(value: float) -> str:
    return f"{value:.1f}%"


def _read_profile() -> dict:
    if not PROFILE_PATH.exists():
        sys.exit(f"profile.yaml not found at {PROFILE_PATH}")
    with PROFILE_PATH.open() as f:
        return yaml.safe_load(f) or {}


def _write_cash_value(new_value: float) -> bool:
    """Replace the investable_cash_pkr line in-place, preserving comments/whitespace."""
    text = PROFILE_PATH.read_text()
    if not CASH_LINE_RE.search(text):
        return False
    new_int = int(round(new_value))
    new_text = CASH_LINE_RE.sub(
        lambda m: f"{m.group('indent')}{CASH_KEY}: {new_int}{m.group('trailing')}",
        text,
        count=1,
    )
    PROFILE_PATH.write_text(new_text)
    return True


def _live_position_value(profile: dict) -> tuple[float, list[tuple[str, float, float]]]:
    holdings = profile.get("holdings") or []
    rows: list[tuple[str, float, float]] = []
    total = 0.0
    for h in holdings:
        ticker = h.get("ticker")
        shares = float(h.get("shares") or 0)
        if not ticker or shares <= 0:
            continue
        # get_price_data uses yfinance for OHLC; suppress noisy stderr from downloads.
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            price_info = get_price_data(ticker)
        price = price_info.get("current_price")
        if price is None:
            print(f"  ! could not fetch price for {ticker}, falling back to avg cost")
            price = float(h.get("avg_cost_pkr") or 0)
        market_value = shares * float(price)
        rows.append((ticker, shares, market_value))
        total += market_value
    return total, rows


def _prompt_float(prompt: str, default: float | None) -> float:
    suffix = f" [{default:.0f}]" if default is not None else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            return float(raw.replace(",", ""))
        except ValueError:
            print(f"  ! '{raw}' is not a number, try again")


def _print_reconciliation(
    *,
    positions_value: float,
    profile_cash: float,
    new_broker_cash: float,
    new_external_cash: float,
    risk: dict,
) -> tuple[float, float]:
    new_total_cash = new_broker_cash + new_external_cash
    old_equity = positions_value + profile_cash
    new_equity = positions_value + new_total_cash
    delta = new_total_cash - profile_cash

    min_cash_pct = float(risk.get("min_cash_reserve_pct") or 0)
    new_cash_pct = (new_total_cash / new_equity * 100) if new_equity else 0.0
    old_cash_pct = (profile_cash / old_equity * 100) if old_equity else 0.0

    print()
    print("== Reconciliation ==")
    print(f"  Positions (live): {_money(positions_value)}")
    print(f"  Cash in broker:   {_money(new_broker_cash)}")
    print(f"  External cash:    {_money(new_external_cash)}")
    print(f"  Broker total worth (positions + broker cash): "
          f"{_money(positions_value + new_broker_cash)}")
    print()
    print(f"  Old investable_cash_pkr: {_money(profile_cash)}  -> equity {_money(old_equity)} "
          f"(cash {_pct(old_cash_pct)})")
    print(f"  New investable_cash_pkr: {_money(new_total_cash)}  -> equity {_money(new_equity)} "
          f"(cash {_pct(new_cash_pct)})")
    sign = "+" if delta >= 0 else ""
    print(f"  Delta: {sign}{_money(delta)}")
    print()
    if new_cash_pct < min_cash_pct:
        print(f"  WARNING: cash reserve {_pct(new_cash_pct)} would be below "
              f"min_cash_reserve_pct {min_cash_pct:.0f}%")
        print(f"           agent will refuse new ENTER/ADD recommendations until you "
              f"either deposit cash or trim a concentrated position")
    elif new_cash_pct < min_cash_pct + 5:
        print(f"  Note: cash reserve {_pct(new_cash_pct)} is close to the "
              f"{min_cash_pct:.0f}% floor; little room for new BUYs")

    risk_max_single = float(risk.get("max_single_position_pct") or 100)
    print()
    print("== Concentration check ==")
    return new_total_cash, new_equity


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--broker-cash", type=float,
                   help="What's in your broker's ledger right now (PKR).")
    p.add_argument("--external-cash", type=float, default=None,
                   help="External cash (bank/savings) you'd realistically deploy (PKR). "
                        "Defaults to 0 if --broker-cash is given.")
    p.add_argument("--write", action="store_true",
                   help="Skip confirmation and write the new value to profile.yaml.")
    args = p.parse_args()

    profile = _read_profile()
    capital = profile.get("capital") or {}
    risk = profile.get("risk") or {}
    profile_cash = float(capital.get(CASH_KEY) or 0)

    print("Fetching live position prices (PSX DPS spot via get_price_data)...")
    positions_value, rows = _live_position_value(profile)
    print()
    print("== Holdings ==")
    for ticker, shares, value in rows:
        print(f"  {ticker:<6} {int(shares):>4} sh   {_money(value):>10}")
    print(f"  {'TOTAL':<6} {' ':>4}      {_money(positions_value):>10}")

    if args.broker_cash is not None:
        broker_cash = args.broker_cash
        external_cash = args.external_cash if args.external_cash is not None else 0.0
    else:
        print()
        print("Enter the cash figures from your broker app + bank.")
        print(f"(Profile currently assumes investable_cash_pkr = {_money(profile_cash)})")
        broker_cash = _prompt_float(
            "  Broker ledger balance (PKR)",
            default=None if profile_cash == 0 else 0,
        )
        external_cash = _prompt_float(
            "  External cash you'd deploy this month (PKR)",
            default=0,
        )

    new_total_cash, new_equity = _print_reconciliation(
        positions_value=positions_value,
        profile_cash=profile_cash,
        new_broker_cash=broker_cash,
        new_external_cash=external_cash,
        risk=risk,
    )

    risk_max_single = float(risk.get("max_single_position_pct") or 100)
    breached = False
    for ticker, shares, value in rows:
        weight = (value / new_equity * 100) if new_equity else 0.0
        flag = "  <-- BREACH" if weight > risk_max_single else ""
        print(f"  {ticker:<6}  weight {_pct(weight)}{flag}")
        if weight > risk_max_single:
            breached = True
    if breached:
        print(f"  (cap is {risk_max_single:.0f}% per single position)")

    print()
    if abs(new_total_cash - profile_cash) < 1:
        print("Profile already matches. Nothing to write.")
        return 0

    if args.write:
        confirm = "y"
    else:
        confirm = input(
            f"Update profile.yaml: {CASH_KEY} {int(profile_cash)} -> {int(round(new_total_cash))} ? [y/N] "
        ).strip().lower()

    if confirm != "y":
        print("Aborted. Profile unchanged.")
        return 1

    if not _write_cash_value(new_total_cash):
        print(f"Could not find '{CASH_KEY}:' line in profile.yaml. Edit manually.")
        return 1
    print(f"Updated profile.yaml: {CASH_KEY} = {int(round(new_total_cash))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
