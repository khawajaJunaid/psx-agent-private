"""
Generate a readable Markdown report from db.sqlite.
Writes reports/YYYYMMDD-HHMMSS.md and reports/LATEST.md

By default the written report includes **only the latest agent run** (one batch
per ``agent.py`` invocation: rows are grouped until a ticker repeats, since each
run analyzes each watchlist symbol at most once). Full DB history is available
with ``python3 report.py --full``.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from logger import get_conn, DB_PATH

REPORTS_DIR = Path(__file__).parent / "reports"
VERDICT_INJECT_CACHE = Path(__file__).parent / ".cache" / "report_verdict_inject.md"


def persist_verdict_inject(markdown: str) -> None:
    """Save portfolio verdict + primary continuity from the last ``agent.py`` run.

    ``python3 report.py`` merges this file when regenerating markdown so LATEST
    is not stripped of the verdict block.
    """
    md = (markdown or "").strip()
    if not md:
        return
    VERDICT_INJECT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    VERDICT_INJECT_CACHE.write_text(md + "\n", encoding="utf-8")


def load_verdict_inject() -> str:
    try:
        if VERDICT_INJECT_CACHE.exists():
            return VERDICT_INJECT_CACHE.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def merge_verdict_into_body(body: str, inject_md: str) -> str:
    inject_md = (inject_md or "").strip()
    if not inject_md:
        return body
    inject = inject_md + "\n\n---\n\n"
    marker = "## Summary"
    if marker in body:
        return body.replace(marker, inject + marker, 1)
    return inject + body


def _rows_decisions():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM decisions ORDER BY timestamp ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def _rows_outcomes():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT d.ticker, d.decision, d.timestamp AS decided_at, d.price_at_decision,
                   o.pnl_pct, o.n_days, o.price_after_nd, o.recorded_at
            FROM decisions d
            JOIN outcomes o ON d.id = o.decision_id
            ORDER BY o.recorded_at DESC
        """).fetchall()
    return [dict(r) for r in rows]


def _agent_runs(rows):
    """Split chronological decision rows into one list per agent invocation.

    When a ticker appears again, the current batch is closed and a new run
    starts. Matches ``agent.py`` (each ticker at most once per loop).
    """
    if not rows:
        return []
    batches = []
    cur = []
    seen = set()
    for row in rows:
        tk = row["ticker"]
        if tk in seen:
            batches.append(cur)
            cur = [row]
            seen = {tk}
        else:
            cur.append(row)
            seen.add(tk)
    if cur:
        batches.append(cur)
    return batches


def _sized_buy_primary_from_batch(batch):
    """Best-effort primary BUY from one batch: sized ENTER/ADD, else highest-conf ENTER/ADD."""
    if not batch:
        return None
    best = None
    best_shares = -1
    for d in batch:
        if d.get("decision") not in ("ENTER", "ADD"):
            continue
        try:
            s = json.loads(d.get("signals") or "{}")
            ad = s.get("action_details") or {}
            sh = int(ad.get("shares") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            ad, sh = {}, 0
        if sh > best_shares:
            best_shares = sh
            best = {
                "ticker": d["ticker"],
                "action": d["decision"],
                "confidence": d.get("confidence"),
                "shares": sh,
                "size_pkr": float(ad.get("size_pkr") or 0),
                "reasoning_excerpt": (d.get("reasoning") or "")[:450],
                "timestamp_utc": d["timestamp"],
            }
    if best is not None and best_shares > 0:
        return best
    # Legacy rows (no code sizing): pick strongest ENTER/ADD by confidence
    fallback = None
    best_conf = -1.0
    for d in batch:
        if d.get("decision") not in ("ENTER", "ADD"):
            continue
        c = float(d.get("confidence") or 0)
        if c >= best_conf:
            best_conf = c
            try:
                ad = json.loads(d.get("signals") or "{}").get("action_details") or {}
            except (json.JSONDecodeError, TypeError, ValueError):
                ad = {}
            fallback = {
                "ticker": d["ticker"],
                "action": d["decision"],
                "confidence": d.get("confidence"),
                "shares": int(ad.get("shares") or 0),
                "size_pkr": float(ad.get("size_pkr") or 0),
                "reasoning_excerpt": (d.get("reasoning") or "")[:450],
                "timestamp_utc": d["timestamp"],
                "legacy_unsized": True,
            }
    return fallback


def get_last_completed_primary():
    """Sized (or best ENTER/ADD) primary from the latest **completed** batch in ``db.sqlite``.

    Call **before** logging a new run so this reflects the previous session.
    """
    if not DB_PATH.exists():
        return None
    rows = _rows_decisions()
    batches = _agent_runs(rows)
    if not batches:
        return None
    return _sized_buy_primary_from_batch(batches[-1])


def _company_map():
    try:
        from watchlist import WATCHLIST

        return dict(WATCHLIST)
    except Exception:
        return {}


def _profile():
    try:
        from tools.profile import load_profile

        return load_profile()
    except Exception:
        return None


def _holding_tickers(profile):
    if not profile:
        return frozenset()
    out = set()
    for h in profile.get("holdings") or []:
        t = h.get("ticker")
        if t:
            out.add(t)
    return frozenset(out)


def _live_portfolio(profile, decisions):
    """Reconstruct the latest portfolio snapshot from the most recent batch's
    stored signals (we cached current_price under signals.price.current_price)."""
    if not profile:
        return None
    try:
        from tools.portfolio import build_portfolio
    except Exception:
        return None
    batches = _agent_runs(decisions)
    latest = batches[-1] if batches else []
    price_by_ticker = {}
    for d in latest:
        try:
            s = json.loads(d.get("signals") or "{}")
        except json.JSONDecodeError:
            continue
        price = s.get("price") or {}
        if isinstance(price, dict) and price.get("current_price") is not None:
            price_by_ticker[d["ticker"]] = price
    return build_portfolio(profile, price_by_ticker)


def _signal_lines(signals_json):
    if not signals_json:
        return ["_No signals stored._"]
    try:
        s = json.loads(signals_json)
    except json.JSONDecodeError:
        return [f"`{signals_json[:200]}…`"]
    lines = []
    price = s.get("price") or {}
    if isinstance(price, dict):
        if price.get("error"):
            lines.append(f"- **Price:** error — {price.get('error')}")
        else:
            src = price.get("price_source")
            src_bit = f" · _source: {src}_" if src else ""
            ba = ""
            if price.get("psx_bid") is not None and price.get("psx_ask") is not None:
                ba = (
                    f" · PSX bid/ask {price.get('psx_bid')}/"
                    f"{price.get('psx_ask')}"
                )
            lines.append(
                f"- **Price:** Rs {price.get('current_price')} · "
                f"30d trend {price.get('trend_30d_pct')}% · "
                f"RSI {price.get('rsi_14')} ({price.get('rsi_signal')}) · "
                f"volume {price.get('volume_signal')}{ba}{src_bit}"
            )
    sent = s.get("sentiment") or {}
    if isinstance(sent, dict):
        themes = sent.get("key_themes") or []
        th = ", ".join(themes) if themes else "—"
        lines.append(
            f"- **Sentiment:** {sent.get('sentiment')} (score {sent.get('score')}) — "
            f"{sent.get('summary', '')}"
        )
        lines.append(f"- **Themes:** {th}")
    return lines if lines else ["_Signals present but unparsed._"]


def _action_block(signals_json, decision_str):
    """Return list of lines describing the action (size, stop, target, impact)."""
    lines = []
    if not signals_json:
        return lines
    try:
        s = json.loads(signals_json)
    except json.JSONDecodeError:
        return lines
    a = s.get("action_details") or {}
    if not a:
        return lines

    action = a.get("action") or decision_str
    shares = a.get("shares")
    size = a.get("size_pkr")
    sl = a.get("stop_loss_pkr")
    tp = a.get("take_profit_pkr")
    horizon = a.get("time_horizon")
    impact = a.get("portfolio_impact")
    risks = a.get("key_risks") or []
    held = a.get("held_at_decision")

    parts = [f"**{action}**"]
    if shares:
        parts.append(f"{int(shares)} shares")
    if size:
        parts.append(f"Rs {float(size):,.0f}")
    if horizon:
        parts.append(f"horizon: {horizon}")
    lines.append("- **Action:** " + " · ".join(parts))

    note = a.get("sizing_note")
    if note:
        lines.append(f"- **Sizing note:** {note}")
    ss = a.get("system_sizing")
    if isinstance(ss, dict) and ss.get("deployable_cash_pkr") is not None:
        floor = ss.get("min_cash_floor_pkr")
        dep = ss.get("deployable_cash_pkr")
        cap = ss.get("capped_by")
        cap_s = ", ".join(cap) if isinstance(cap, list) else cap
        lines.append(
            f"- **Cash / caps:** deployable Rs {float(dep):,.0f} after min cash floor "
            f"Rs {float(floor or 0):,.0f}; binding limit(s): {cap_s or '—'}."
        )
        lines.append(
            "- **Why this share count?** Size is the **minimum** of: (1) cash after your "
            "**min cash reserve %**, (2) room under **max single-position %** of equity, "
            "(3) **sector exposure %**. You can have plenty of deployable cash but still "
            "only buy a few shares if the position cap is the tightest rule — raise "
            "`max_single_position_pct` in `profile.yaml` only if that matches your risk plan."
        )
        caps = cap if isinstance(cap, list) else []
        ms = ss.get("max_spend_pkr")
        if ms is not None and "single-name cap" in caps:
            dep_f = float(dep or 0)
            ms_f = float(ms)
            if dep_f > ms_f + 0.01:
                lines.append(
                    f"- **Not the full Rs {dep_f:,.0f} cash:** this trade is capped at ≈ Rs {ms_f:,.0f} "
                    "by **max single-position %** (new position must stay within that slice of total equity)."
                )

    if sl is not None or tp is not None:
        lines.append(f"- **Stop loss:** Rs {sl}  ·  **Take profit:** Rs {tp}")
    if held:
        lines.append(
            f"- **Position at decision:** {int(held.get('shares', 0))} shares @ "
            f"avg Rs {held.get('avg_cost_pkr')}, P&L Rs {held.get('pnl_pkr')} "
            f"({held.get('pnl_pct'):+.2f}%), weight {held.get('weight_pct')}%"
        )
    if impact:
        lines.append(f"- **Portfolio impact:** {impact}")
    if risks:
        lines.append("- **Key risks:** " + "; ".join(risks))
    return lines


def _portfolio_section(portfolio):
    if not portfolio:
        return []
    lines = [
        "## Current holdings",
        "",
        "_From `profile.yaml` holdings + live prices from this report’s latest run._",
        "",
        f"- **Total equity:** Rs {portfolio['total_equity_pkr']:,.0f}  "
        f"(positions Rs {portfolio['total_market_value_pkr']:,.0f} + "
        f"cash Rs {portfolio['cash_pkr']:,.0f})",
        f"- **Cash:** Rs {portfolio['cash_pkr']:,.0f} ({portfolio['cash_pct']:.1f}%)",
        f"- **Total P&L:** Rs {portfolio['total_pnl_pkr']:,.0f} "
        f"({portfolio['total_pnl_pct']:+.2f}%)",
        "",
    ]
    if portfolio["positions"]:
        lines += [
            "| Ticker | Shares | Avg cost | Current | Mkt value | Weight | P&L | P&L % |",
            "|--------|-------:|---------:|--------:|----------:|-------:|----:|------:|",
        ]
        for p in portfolio["positions"]:
            lines.append(
                f"| {p['ticker']} | {int(p['shares'])} | {p['avg_cost_pkr']} | "
                f"{p['current_price_pkr']} | {p['market_value_pkr']:,.0f} | "
                f"{p['weight_pct']:.1f}% | {p['pnl_pkr']:,.0f} | {p['pnl_pct']:+.2f}% |"
            )
        lines.append("")
    if portfolio["breaches"]:
        lines.append("**Breaches:** " + "; ".join(portfolio["breaches"]))
    else:
        lines.append("**Breaches:** none")
    lines.append("")
    return lines


def _one_decision_detail(d, companies):
    """Markdown lines for a single decision row (detail view)."""
    tk = d["ticker"]
    co = companies.get(tk, "")
    title_co = f" ({co})" if co else ""
    lines = [
        f"#### {tk}{title_co} — {d['decision']}",
        "",
        f"- **When (UTC):** {d['timestamp']}",
        f"- **Confidence:** {d.get('confidence')}",
        f"- **Price at decision:** Rs {d.get('price_at_decision')}",
        f"- **ID:** `{d['id']}`",
        "",
    ]
    lines.extend(_action_block(d.get("signals"), d["decision"]))
    lines.extend(
        [
            "",
            "**Reasoning**",
            "",
            (d.get("reasoning") or "_none_").strip(),
            "",
            "**Signals**",
            "",
        ]
    )
    lines.extend(_signal_lines(d.get("signals")))
    lines.append("")
    return lines


def _split_scope_by_holdings(scope_decisions, profile):
    """Newest-first batches preserved: same order as reversed(scope_decisions)."""
    held_set = _holding_tickers(profile)
    held_rows = []
    other_rows = []
    for d in reversed(scope_decisions):
        if d["ticker"] in held_set:
            held_rows.append(d)
        else:
            other_rows.append(d)
    return held_rows, other_rows


def build_markdown(latest_batch_only=True):
    if not DB_PATH.exists():
        return (
            "# PSX Agent Report\n\n"
            f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} local_\n\n"
            "No database found. Run `python3 agent.py` first.\n"
        )

    all_decisions = _rows_decisions()
    outcomes = _rows_outcomes()
    companies = _company_map()
    profile = _profile()
    portfolio = _live_portfolio(profile, all_decisions)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    batches = _agent_runs(all_decisions)
    latest_batch = batches[-1] if batches else []
    if latest_batch_only:
        scope_decisions = latest_batch
        detail_title = "## This run — recommendations & analysis"
        scope_note = (
            f"_This file lists **only the latest agent run** ({len(scope_decisions)} "
            f"decision(s)). All-time rows in DB: {len(all_decisions)}. "
            f"Full history: `python3 report.py --full`_"
        )
    else:
        scope_decisions = all_decisions
        detail_title = "## All decisions — by holdings vs rest (newest first in each group)"
        scope_note = (
            f"_Full history: **{len(all_decisions)}** decision(s) in `{DB_PATH.name}`._"
        )

    lines = [
        "# PSX Agent — personal report",
        "",
        f"_Generated {now} (local)_ · Database: `{DB_PATH.name}`",
        "",
        scope_note,
        "",
        "---",
        "",
    ]

    if portfolio:
        lines.extend(_portfolio_section(portfolio))
        lines.append("---")
        lines.append("")

    lines.extend(["## Summary", ""])

    if not all_decisions:
        lines.extend(["No decisions logged yet.", ""])
    else:
        counts = {}
        for d in scope_decisions:
            counts[d["decision"]] = counts.get(d["decision"], 0) + 1
        bullets = ", ".join(
            f"{k}={v}"
            for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
        )
        lines.extend(
            [
                "| Metric | Value |",
                "|--------|-------|",
                f"| Decisions in this report | {len(scope_decisions)} |",
            ]
        )
        if latest_batch_only and len(all_decisions) != len(scope_decisions):
            lines.append(f"| All-time decisions in DB | {len(all_decisions)} |")
        lines.extend(
            [
                f"| Action mix (this scope) | {bullets} |",
                f"| Decisions with outcomes (all time) | {len(outcomes)} |",
                "",
            ]
        )

        latest = latest_batch
        if latest:
            t0 = latest[0]["timestamp"][:19].replace("T", " ")
            lines.extend(
                [
                    "### Latest agent run",
                    "",
                    f"_Started ~{t0} UTC · {len(latest)} ticker(s) in this batch._",
                    "",
                    "| Ticker | Action | Shares | Size (Rs) | Conf | Price | Stop | Target |",
                    "|--------|--------|-------:|----------:|-----:|------:|-----:|-------:|",
                ]
            )
            for d in latest:
                tk = d["ticker"]
                action = d["decision"]
                shares = "—"
                size = "—"
                sl = "—"
                tp = "—"
                try:
                    s = json.loads(d.get("signals") or "{}")
                    a = s.get("action_details") or {}
                    if a:
                        sh_val = a.get("shares")
                        sz_val = a.get("size_pkr")
                        if sh_val:
                            shares = f"{int(sh_val)}"
                        if sz_val:
                            size = f"{float(sz_val):,.0f}"
                        if a.get("stop_loss_pkr") is not None:
                            sl = f"{a['stop_loss_pkr']}"
                        if a.get("take_profit_pkr") is not None:
                            tp = f"{a['take_profit_pkr']}"
                except json.JSONDecodeError:
                    pass

                conf = d.get("confidence")
                cp = f"{int(conf * 100)}%" if conf is not None else "—"
                px = d.get("price_at_decision")
                pxs = f"{px:.4g}" if px is not None else "—"
                lines.append(
                    f"| {tk} | {action} | {shares} | {size} | {cp} | {pxs} | {sl} | {tp} |"
                )
            lines.append("")

    if outcomes:
        lines.extend(
            [
                "## Closed outcomes (eval)",
                "",
                "| Ticker | Action | P&L % | Days | Decided (UTC) |",
                "|--------|--------|------:|-----:|----------------|",
            ]
        )
        for o in outcomes:
            pnl = o.get("pnl_pct")
            pnls = f"{pnl:+.2f}" if pnl is not None else "—"
            lines.append(
                f"| {o['ticker']} | {o['decision']} | {pnls} | "
                f"{o.get('n_days', '—')} | {str(o.get('decided_at', ''))[:19]} |"
            )
        lines.append("")

    if scope_decisions:
        lines.extend(["---", "", detail_title, ""])
        if latest_batch_only:
            lines.append(
                "_Holdings = tickers in `profile.yaml`; the rest are watchlist / ideas. "
                "Within each group, order is newest-first (same as the agent run)._"
            )
            lines.append("")
        held_rows, other_rows = _split_scope_by_holdings(scope_decisions, profile)
        if held_rows:
            lines.append("### Names you already hold")
            lines.append("")
            for d in held_rows:
                lines.extend(_one_decision_detail(d, companies))
        if other_rows:
            sub = (
                "### Watchlist & other names (new cash / ideas)"
                if held_rows
                else "### Watchlist & names analyzed"
            )
            lines.append(sub)
            lines.append("")
            for d in other_rows:
                lines.extend(_one_decision_detail(d, companies))

    lines.append("_End of report._\n")
    return "\n".join(lines)


def write_reports(
    quiet=False,
    verdict_section=None,
    latest_batch_only=True,
    merge_cached_inject=True,
):
    """Generate a fresh Markdown report and write both archive + LATEST.md.

    ``verdict_section`` — optional Markdown from ``agent.synthesize_portfolio_verdict``
    (inserted before ``## Summary`` so the report opens with one primary call).

    When ``verdict_section`` is empty and ``merge_cached_inject`` is True, the last
    agent run's block is loaded from ``.cache/report_verdict_inject.md`` (see
    ``persist_verdict_inject``).

    ``latest_batch_only`` — when True (default), detail sections list only the
    most recent agent batch. Use False for full DB history (see ``report.py --full``).

    Returns (archived_path, latest_path). Importable from agent.py so a single
    `python3 agent.py` run produces a refreshed report without a second command.
    """
    body = build_markdown(latest_batch_only=latest_batch_only)
    inject_src = (verdict_section or "").strip()
    if not inject_src and merge_cached_inject:
        inject_src = load_verdict_inject()
    body = merge_verdict_into_body(body, inject_src)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archived = REPORTS_DIR / f"psx-report-{stamp}.md"
    latest = REPORTS_DIR / "LATEST.md"
    archived.write_text(body, encoding="utf-8")
    latest.write_text(body, encoding="utf-8")
    if not quiet:
        print(f"  Report written to {archived.resolve()}")
        print(f"  Also: {latest.resolve()}")
    return archived, latest


def main():
    parser = argparse.ArgumentParser(description="Write Markdown report from db.sqlite")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write to this path only (skip LATEST copy and timestamped file)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Include every decision in the DB (default is latest agent run only)",
    )
    parser.add_argument(
        "--no-inject",
        action="store_true",
        help="Do not merge cached verdict / primary continuity (database sections only)",
    )
    args = parser.parse_args()

    if args.output:
        body = build_markdown(latest_batch_only=not args.full)
        if not args.no_inject:
            body = merge_verdict_into_body(body, load_verdict_inject())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body, encoding="utf-8")
        print(f"  Report written to {args.output.resolve()}")
        return

    write_reports(
        latest_batch_only=not args.full,
        merge_cached_inject=not args.no_inject,
    )


if __name__ == "__main__":
    main()
