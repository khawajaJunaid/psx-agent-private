from __future__ import annotations

import argparse
from datetime import datetime
from logger import get_conn, record_outcome, get_open_decisions, log_execution
from tools.price import get_price_data
from tools.psx_quote import reset_dps_session


def _directional_win(decision: str, pnl_pct: float | None) -> bool | None:
    """Whether price move after the call validates the signal (None = not scored)."""
    if pnl_pct is None:
        return None
    d = (decision or "").upper()
    if d in ("BUY", "ENTER", "ADD"):
        return pnl_pct > 0
    if d in ("SELL", "TRIM", "EXIT"):
        return pnl_pct < 0
    return None


def _resolve_decision_id(ref: str) -> str | None:
    ref = (ref or "").strip()
    if not ref:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM decisions WHERE id = ?", (ref,)).fetchone()
        if row:
            return row["id"]
        rows = conn.execute(
            """
            SELECT id, ticker, decision, timestamp FROM decisions
            WHERE id LIKE ? ORDER BY timestamp DESC LIMIT 15
            """,
            (ref + "%",),
        ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if not rows:
        print("  No matching decision id; execution will not be linked.")
        return None
    print("  Ambiguous id prefix — pick full id from agent output; not linking.")
    for r in rows[:8]:
        print(
            f"    {r['id']}  {r['ticker']} {r['decision']} {str(r['timestamp'])[:10]}"
        )
    return None


def fetch_current_price(ticker):
    d = get_price_data(ticker)
    if d.get("error"):
        return None
    p = d.get("current_price")
    return float(p) if p is not None else None

def compute_metrics():
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT d.ticker, d.decision, d.confidence, d.signals, d.price_at_decision, d.timestamp, o.pnl_pct, o.n_days
            FROM decisions d JOIN outcomes o ON d.id = o.decision_id ORDER BY d.timestamp DESC
        """).fetchall()]
    if not rows:
        print("\n  No outcomes recorded yet. Run with --record first.\n")
        return
    total = len(rows)
    scored = [(r, _directional_win(r["decision"], r["pnl_pct"])) for r in rows]
    directional = [(r, w) for r, w in scored if w is not None]
    wins = sum(1 for _, w in directional if w)
    win_rate = (wins / len(directional)) * 100 if directional else 0.0
    long_rows = [r for r in rows if (r["decision"] or "").upper() in ("BUY", "ENTER", "ADD")]
    avg_long_pnl = (
        sum(r["pnl_pct"] for r in long_rows) / len(long_rows) if long_rows else 0
    )
    print(f"\n{'═'*52}\n  PSX AGENT — EVAL REPORT\n  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'═'*52}")
    print(f"\n  Total decisions scored : {total}")
    print(f"  Directional scored     : {len(directional)} (excludes HOLD, etc.)")
    print(f"  Directional win rate   : {win_rate:.1f}%")
    print(f"  Avg P&L (ENTER/ADD/BUY): {avg_long_pnl:+.2f}%")
    tickers = sorted(set(r["ticker"] for r in rows))
    print(f"\n  {'Ticker':<8} {'Decisions':>9} {'Win rate':>9} {'Avg P&L':>9}\n  {'─'*40}")
    for t in tickers:
        t_rows = [r for r in rows if r["ticker"] == t]
        t_dir = [
            (r, _directional_win(r["decision"], r["pnl_pct"])) for r in t_rows
        ]
        t_scored = [w for _, w in t_dir if w is not None]
        t_wins = sum(1 for w in t_scored if w)
        wr = (t_wins / len(t_scored)) * 100 if t_scored else 0.0
        print(
            f"  {t:<8} {len(t_rows):>9} {wr:>8.1f}% {sum(r['pnl_pct'] for r in t_rows)/len(t_rows):>+8.2f}%"
        )
    print()


def interactive_log_execution():
    """Log shares actually bought/sold in the broker (audit trail; optional link to a decision)."""
    print("\n  Log executed trade (saved to executions table)\n")
    ticker = input("  Ticker (e.g. MEBL): ").strip().upper()
    if not ticker:
        print("  Aborted.")
        return
    action = input("  Action (TRIM / ENTER / ADD / EXIT): ").strip().upper()
    if not action:
        print("  Aborted.")
        return
    q_raw = input("  Shares (quantity, positive): ").strip()
    if not q_raw.isdigit() or int(q_raw) <= 0:
        print("  Need a positive integer share count.")
        return
    quantity = int(q_raw)
    px_raw = input("  Avg execution price PKR (optional, Enter to skip): ").strip()
    price = float(px_raw) if px_raw else None
    did = input("  Decision id from agent log — optional, paste full UUID or unique prefix: ").strip()
    decision_id = _resolve_decision_id(did) if did else None
    notes = input("  Notes (optional): ").strip() or None
    log_execution(
        ticker=ticker,
        action=action,
        quantity=quantity,
        price_pkr=price,
        decision_id=decision_id,
        notes=notes,
    )
    print("  Done.\n")


def compute_execution_attribution():
    """How agent quality looks on *executed* recommendations only."""
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT
                    d.id,
                    d.ticker,
                    d.decision,
                    d.timestamp,
                    o.pnl_pct,
                    o.n_days,
                    EXISTS(SELECT 1 FROM executions e WHERE e.decision_id = d.id) AS was_executed
                FROM decisions d
                LEFT JOIN outcomes o ON o.decision_id = d.id
                ORDER BY d.timestamp DESC
                """
            ).fetchall()
        ]
        exec_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT ticker, action, quantity, price_pkr, decision_id, executed_at
                FROM executions
                ORDER BY executed_at DESC
                """
            ).fetchall()
        ]

    if not rows:
        print("\n  No decisions found yet.\n")
        return

    directional_all = [
        (r, _directional_win(r["decision"], r["pnl_pct"]))
        for r in rows
        if _directional_win(r["decision"], r["pnl_pct"]) is not None
    ]
    directional_exec = [
        (r, _directional_win(r["decision"], r["pnl_pct"]))
        for r in rows
        if r.get("was_executed")
        and _directional_win(r["decision"], r["pnl_pct"]) is not None
    ]
    wins_all = sum(1 for _, w in directional_all if w)
    wins_exec = sum(1 for _, w in directional_exec if w)
    wr_all = (wins_all / len(directional_all) * 100) if directional_all else 0.0
    wr_exec = (wins_exec / len(directional_exec) * 100) if directional_exec else 0.0

    print(f"\n{'═'*52}\n  PSX AGENT — EXECUTION ATTRIBUTION\n  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'═'*52}")
    print(f"\n  Total decisions                 : {len(rows)}")
    print(f"  Decisions linked to executions  : {sum(1 for r in rows if r.get('was_executed'))}")
    print(f"  Directional win rate (all)      : {wr_all:.1f}%  [{wins_all}/{len(directional_all)}]")
    print(f"  Directional win rate (executed) : {wr_exec:.1f}%  [{wins_exec}/{len(directional_exec)}]")

    pending_exec = [
        r for r in rows if r.get("was_executed") and r.get("pnl_pct") is None
    ]
    if pending_exec:
        print(f"\n  Executed decisions awaiting outcome: {len(pending_exec)}")
        for r in pending_exec[:12]:
            print(f"    {r['ticker']:<7} {r['decision']:<6} {str(r['timestamp'])[:10]}  id={r['id'][:8]}...")

    if exec_rows:
        print(f"\n  Recent execution log entries: {min(10, len(exec_rows))}")
        for e in exec_rows[:10]:
            px = f"Rs {float(e['price_pkr']):.2f}" if e.get("price_pkr") is not None else "px n/a"
            link = (e.get("decision_id") or "")[:8]
            suffix = f"  -> {link}..." if link else ""
            print(
                f"    {str(e['executed_at'])[:19]}  {e['ticker']:<7} {e['action']:<6} x{e['quantity']:<4} {px}{suffix}"
            )
    print()


def compute_weekly_execution_report(days: int = 7):
    """Weekly review: executed flow, turnover proxy, and scored quality."""
    if days <= 0:
        raise SystemExit("--weekly-days must be > 0")
    with get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT
                    e.executed_at,
                    e.ticker,
                    e.action,
                    e.quantity,
                    e.price_pkr,
                    e.decision_id,
                    d.decision AS model_decision,
                    d.price_at_decision,
                    o.pnl_pct,
                    o.n_days
                FROM executions e
                LEFT JOIN decisions d ON d.id = e.decision_id
                LEFT JOIN outcomes o ON o.decision_id = e.decision_id
                WHERE datetime(e.executed_at) >= datetime('now', '-{int(days)} days')
                ORDER BY e.executed_at DESC
                """
            ).fetchall()
        ]
    print(
        f"\n{'═'*52}\n  PSX AGENT — WEEKLY EXECUTION REVIEW\n  {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'═'*52}"
    )
    print(f"\n  Window: last {days} days")
    print(f"  Executions logged: {len(rows)}")
    if not rows:
        print()
        return
    notional = 0.0
    scored = []
    linked = 0
    # Approximate trade contribution vs "do nothing" for executed actions.
    # Positive means execution improved P&L vs not trading.
    what_if_rows = []
    for r in rows:
        q = float(r.get("quantity") or 0)
        px = r.get("price_pkr")
        if px is not None:
            notional += abs(q * float(px))
        if r.get("decision_id"):
            linked += 1
        w = _directional_win(r.get("model_decision") or "", r.get("pnl_pct"))
        if w is not None:
            scored.append((r, w))
        act = (r.get("action") or "").upper()
        exec_px = float(px) if px is not None else None
        if exec_px is None and r.get("price_at_decision") is not None:
            exec_px = float(r["price_at_decision"])
        if act in ("ENTER", "ADD", "TRIM", "EXIT") and exec_px is not None and q > 0:
            now_px = fetch_current_price(r.get("ticker") or "")
            if now_px is not None:
                if act in ("ENTER", "ADD"):
                    delta = q * (now_px - exec_px)
                else:  # TRIM / EXIT
                    delta = q * (exec_px - now_px)
                what_if_rows.append(
                    {
                        "ticker": r.get("ticker"),
                        "action": act,
                        "qty": q,
                        "exec_px": exec_px,
                        "now_px": now_px,
                        "delta_pkr": delta,
                    }
                )
    wins = sum(1 for _, w in scored if w)
    wr = (wins / len(scored) * 100) if scored else 0.0
    print(f"  Linked to decisions: {linked}")
    print(f"  Approx traded notional (known px): Rs {notional:,.0f}")
    print(f"  Scored executed calls: {len(scored)}")
    print(f"  Win rate on scored executed calls: {wr:.1f}%")

    pending = [r for r in rows if r.get("decision_id") and r.get("pnl_pct") is None]
    if pending:
        print(f"  Pending outcomes: {len(pending)}")
    if what_if_rows:
        total_delta = sum(x["delta_pkr"] for x in what_if_rows)
        print(
            f"  Trade timing effect vs no-trade (approx): Rs {total_delta:+,.0f} "
            "(+ means executions helped)"
        )
    print("\n  Recent executions:")
    for r in rows[:12]:
        px = f"Rs {float(r['price_pkr']):.2f}" if r.get("price_pkr") is not None else "px n/a"
        did = (r.get("decision_id") or "")[:8]
        pnl = r.get("pnl_pct")
        pnl_txt = f"{float(pnl):+.2f}%" if pnl is not None else "pnl n/a"
        print(
            f"    {str(r['executed_at'])[:19]}  {r['ticker']:<7} {r['action']:<6} x{int(r['quantity'] or 0):<4} {px}  {pnl_txt}  {did+'...' if did else ''}"
        )
    if what_if_rows:
        print("\n  What-if no-trade by execution (approx):")
        for x in what_if_rows[:12]:
            print(
                f"    {x['ticker']:<7} {x['action']:<6} x{int(x['qty']):<4} "
                f"exec Rs {x['exec_px']:.2f} -> now Rs {x['now_px']:.2f}  "
                f"impact Rs {x['delta_pkr']:+,.0f}"
            )
    print()

def interactive_record():
    reset_dps_session()
    open_decisions = get_open_decisions()
    if not open_decisions:
        print("\n  No open decisions to record.\n")
        return
    for d in open_decisions:
        print(f"\n  {d['ticker']} {d['decision']} Rs {d['price_at_decision']} on {d['timestamp'][:10]}")
        current = fetch_current_price(d["ticker"])
        if current:
            print(f"  Current price: Rs {current:.2f}")
            if input("  Use this as outcome? (y/n): ").strip().lower() == "y":
                n_days = int(input("  Days since decision? "))
                record_outcome(d["id"], current, n_days)
                continue
        price_str = input(f"  Enter outcome price manually (or skip): ").strip()
        if price_str:
            record_outcome(d["id"], float(price_str), int(input("  Days since decision? ")))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Score past agent calls: --record attaches a price to an open decision. "
            "--log-execution stores what you actually traded (optional link to that decision)."
        )
    )
    parser.add_argument("--record", action="store_true", help="Record outcome price for an open decision")
    parser.add_argument(
        "--log-execution",
        action="store_true",
        help="Append a broker execution row (trim/buy/exit) to db.sqlite",
    )
    parser.add_argument(
        "--attribution",
        action="store_true",
        help="Compare all-vs-executed decision quality using linked executions",
    )
    parser.add_argument(
        "--weekly",
        action="store_true",
        help="Weekly execution review (turnover + scored executed calls)",
    )
    parser.add_argument(
        "--weekly-days",
        type=int,
        default=7,
        help="Window in days for --weekly (default: 7)",
    )
    parser.add_argument("--exec-ticker", help="Non-interactive execution ticker (requires --log-execution)")
    parser.add_argument("--exec-action", help="Non-interactive execution action (TRIM/ENTER/ADD/EXIT)")
    parser.add_argument("--exec-qty", type=int, help="Non-interactive execution quantity (positive int)")
    parser.add_argument("--exec-price", type=float, default=None, help="Non-interactive execution avg price")
    parser.add_argument(
        "--exec-decision-id",
        default=None,
        help="Decision id or unique prefix to link execution",
    )
    parser.add_argument("--exec-notes", default=None, help="Optional note for non-interactive execution")
    args = parser.parse_args()
    if args.weekly:
        compute_weekly_execution_report(args.weekly_days)
    elif args.attribution:
        compute_execution_attribution()
    elif args.log_execution:
        if args.exec_ticker and args.exec_action and args.exec_qty:
            if args.exec_qty <= 0:
                raise SystemExit("--exec-qty must be a positive integer")
            did = _resolve_decision_id(args.exec_decision_id) if args.exec_decision_id else None
            log_execution(
                ticker=args.exec_ticker.strip().upper(),
                action=args.exec_action.strip().upper(),
                quantity=int(args.exec_qty),
                price_pkr=args.exec_price,
                decision_id=did,
                notes=args.exec_notes,
            )
        else:
            interactive_log_execution()
    elif args.record:
        interactive_record()
    else:
        compute_metrics()
