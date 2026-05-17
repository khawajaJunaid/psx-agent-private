import json
import os
from datetime import datetime, timezone
from dotenv import load_dotenv

from tools.price import get_price_data
from tools.news import get_news_headlines
from tools.psx_announcements import get_announcements
from tools.sentiment import analyse_sentiment
from tools.llm import complete
from tools.profile import apply_price_overrides, load_profile, get_holding
from tools.portfolio import build_portfolio, compute_buy_allocation, position_for
from tools.psx_quote import reset_dps_session
from logger import get_conn, init_db, log_decision
from report import get_last_completed_primary
from watchlist import WATCHLIST, TICKER_SECTOR, watchlist_sector_map_lines

load_dotenv()


SYSTEM_PROMPT_TEMPLATE = """
You are a personal portfolio analyst for the Pakistan Stock Exchange (PSX).
You analyse a single stock at a time, in the context of the user's profile,
goals, and existing portfolio. You are conservative, evidence-driven, and
honest about uncertainty. You DO NOT promise returns.
Do not claim an AI knowledge cutoff or "data up to" a past year in reasoning — the user
message includes prices, news, and PSX filings fetched for this run.

Action vocabulary:
- ENTER : open a NEW position (user does not currently hold this ticker)
- ADD   : buy more of an EXISTING position
- HOLD  : do nothing this cycle
- TRIM  : sell PART of an existing position
- EXIT  : sell ALL of an existing position

Decision rules:
- Goal style: {goal_style} — {goal_notes}
- Risk tolerance: {risk_tolerance}; user's pain threshold ~{max_dd}% drawdown
- Caps: single position <= {max_single}% of total equity, sector <= {max_sector}%, keep >= {min_cash}% cash
- Shariah preference: {shariah}
- Available cash for BUYs: Rs {cash_pkr:.0f}. Never recommend ENTER/ADD with size_pkr greater than this.
- For tickers user already HOLDS: weigh current P&L; do not panic-sell small unrealised losses, but TRIM/EXIT if signal weakens AND position is over-concentrated.
- For tickers user does NOT hold: ENTER only if signal is strong AND it fits portfolio (sector/cash caps).
  You may only recommend ENTER for tickers that appear in the user's watchlist (the prompt lists them by sector).
- Macro / sector themes: The watchlist spans banking, cement, fertilizer, E&P, OMC, power, IT, autos, FMCG, etc.
  When **news** (not guesswork) points to a clear sector tailwind or headwind — e.g. large-scale reconstruction or
  infra spend lifting **cement** demand; **fertilizer** or gas-policy headlines; **oil** moves affecting E&P/OMC;
  monetary or regulatory shifts affecting **banks** — you may lean ENTER or ADD for names in that sector **only if**
  price, RSI/trend, and **official PSX filings** still support the case and sizing respects caps and cash.
  State the theme explicitly in reasoning when it drives the call; never ENTER on theme alone with weak or conflicting data.
- RSI < 35 oversold, > 70 overbought; combine with sentiment and trend.
- Treat OFFICIAL PSX ANNOUNCEMENTS as the strongest signal (causal events from the company itself):
    * READ the "--- filing text ---" blocks when present. These are the actual contents
      of the company's official letter to PSX. Extract concrete facts:
      dividend amount (Rs/share or %), book closure dates, board meeting agenda,
      reported earnings/loss numbers, and reflect them in your reasoning.
    * Recent dividend / payout / book closure -> bullish bias for income holders;
      cite the rupee amount and dates when you reason
    * Board meetings scheduled soon -> upcoming catalyst, raise confidence cautiously
    * Negative regulatory notices, defaults, suspensions, AGM controversies -> lean SELL
    * If "(content unavailable...)" appears, the PDF is scanned -- fall back to the title
    * No filings in 30d is normal -> do not penalise
- When signals conflict: HOLD with low confidence.
- **Scarcity:** The run ends with a single "portfolio verdict" that picks at most **one** primary use of new cash.
  Prefer **HOLD** over **ENTER** unless the case is clearly strong; do not mark every "reasonable" name as ENTER.

Sizing (executable amounts — you do not decide rupees or share count):
- For ENTER/ADD always output "shares": 0 and "size_pkr": 0. The runtime computes
  exact shares from **investable cash** in the portfolio snapshot, **min cash reserve**,
  **max single-position %**, and **sector cap** vs current holdings.
- For TRIM/EXIT, output integer shares and size_pkr as before (partial or full sell).
- stop_loss_pkr / take_profit_pkr: per-share PKR levels when you recommend a buy; null for HOLD.

Respond ONLY with this JSON (no markdown, no commentary):
{{"action": "ENTER" | "ADD" | "HOLD" | "TRIM" | "EXIT",
  "shares": <integer>,
  "size_pkr": <number>,
  "stop_loss_pkr": <number or null>,
  "take_profit_pkr": <number or null>,
  "confidence": <0.0-1.0>,
  "reasoning": "<2-4 sentences referencing signals AND the user's portfolio context>",
  "portfolio_impact": "<one sentence: how this affects the user's allocation, concentration, or cash>",
  "key_risks": ["<risk1>", "<risk2>"],
  "time_horizon": "short" | "medium" | "long"}}
""".strip()


def build_system_prompt(profile):
    if not profile:
        return SYSTEM_PROMPT_TEMPLATE.format(
            goal_style="growth",
            goal_notes="No profile loaded; treat as generic growth investor.",
            risk_tolerance="medium",
            max_dd=25,
            max_single=20,
            max_sector=40,
            min_cash=10,
            shariah="none",
            cash_pkr=0,
        )
    goal = profile.get("goal") or {}
    risk = profile.get("risk") or {}
    capital = profile.get("capital") or {}
    return SYSTEM_PROMPT_TEMPLATE.format(
        goal_style=goal.get("style", "growth"),
        goal_notes=goal.get("notes", ""),
        risk_tolerance=risk.get("tolerance", "medium"),
        max_dd=risk.get("max_drawdown_pct", 25),
        max_single=risk.get("max_single_position_pct", 20),
        max_sector=risk.get("max_sector_exposure_pct", 40),
        min_cash=risk.get("min_cash_reserve_pct", 10),
        shariah=risk.get("shariah_preference", "none"),
        cash_pkr=float(capital.get("investable_cash_pkr") or 0),
    )


def _portfolio_lines(portfolio):
    if not portfolio:
        return "  (no profile loaded)"
    lines = []
    lines.append(
        f"  Total equity: Rs {portfolio['total_equity_pkr']:.0f} "
        f"(positions Rs {portfolio['total_market_value_pkr']:.0f} + cash Rs {portfolio['cash_pkr']:.0f})"
    )
    lines.append(
        f"  Cash: Rs {portfolio['cash_pkr']:.0f} ({portfolio['cash_pct']:.1f}%)  "
        f"Total P&L: Rs {portfolio['total_pnl_pkr']:.0f} ({portfolio['total_pnl_pct']:+.2f}%)"
    )
    if portfolio["positions"]:
        lines.append("  Positions:")
        for p in portfolio["positions"]:
            lines.append(
                f"    - {p['ticker']:<6} {int(p['shares'])} sh @ Rs {p['avg_cost_pkr']} "
                f"| now Rs {p['current_price_pkr']} | mkt Rs {p['market_value_pkr']:.0f} "
                f"({p['weight_pct']:.1f}%) | P&L Rs {p['pnl_pkr']:.0f} ({p['pnl_pct']:+.2f}%)"
            )
    if portfolio["breaches"]:
        lines.append("  Breaches: " + "; ".join(portfolio["breaches"]))
    else:
        lines.append("  Breaches: none")
    return "\n".join(lines)


def _format_announcements(announcements):
    if not announcements or announcements.get("error"):
        err = (announcements or {}).get("error")
        return f"  (none available{': ' + err if err else ''})"
    items = announcements.get("announcements") or []
    if not items:
        return "  (no filings in the last 30 days)"
    lines = []
    for it in items:
        lines.append(f"  - {it['date']}  [{it['category']}]  {it['title']}")
        content = (it.get("content") or "").strip()
        if content:
            method = it.get("content_method", "text-layer")
            label = "filing text" if method == "text-layer" else "filing summary (vision OCR)"
            indented = "\n".join("      " + ln for ln in content.splitlines() if ln.strip())
            lines.append(f"      --- {label} ---")
            lines.append(indented)
            lines.append(f"      --- end {label} ---")
        elif it.get("content_error"):
            lines.append(f"      (content unavailable: {it['content_error']})")
    return "\n".join(lines)


def _format_headlines(news, max_lines=8):
    if not news:
        return "  (none)"
    lines = news.get("headlines") or []
    if not lines:
        err = news.get("error")
        if err:
            return f"  (fetch issue: {err})"
        return "  (none — see sentiment summary)"
    out = []
    for h in lines[:max_lines]:
        out.append(f"  - {h}")
    return "\n".join(out)


def build_user_prompt(ticker, company_name, price, sentiment, announcements, profile, portfolio, news=None):
    held = position_for(portfolio, ticker)
    if held:
        position_block = (
            f"USER HOLDS this ticker: {int(held['shares'])} shares @ avg Rs {held['avg_cost_pkr']} "
            f"(cost Rs {held['cost_basis_pkr']:.0f}) | current value Rs {held['market_value_pkr']:.0f} "
            f"({held['weight_pct']:.1f}% of equity) | P&L Rs {held['pnl_pkr']:.0f} ({held['pnl_pct']:+.2f}%)"
        )
    else:
        position_block = "USER does NOT currently hold this ticker."

    profile_block = "(no profile loaded)"
    if profile:
        cf = profile.get("cash_flow") or {}
        cap = profile.get("capital") or {}
        profile_block = (
            f"  Monthly take-home: Rs {cf.get('monthly_take_home', 0):.0f} | "
            f"fixed expenses: Rs {cf.get('monthly_fixed_expenses', 0):.0f}\n"
            f"  Investable cash: Rs {cap.get('investable_cash_pkr', 0):.0f} | "
            f"monthly add: Rs {cap.get('monthly_contribution_pkr', 0):.0f}"
        )

    portfolio_block = _portfolio_lines(portfolio)
    ticker_sector = TICKER_SECTOR.get(ticker, "See company profile / PSX sector")
    sector_map = watchlist_sector_map_lines()

    return f"""Stock: {ticker} ({company_name})

== USER PROFILE ==
{profile_block}

== PORTFOLIO SNAPSHOT ==
{portfolio_block}

== USER'S POSITION IN {ticker} ==
{position_block}

== THIS TICKER (sector for thematic context) ==
- {ticker}: sector = {ticker_sector}

== WATCHLIST BY SECTOR (finite universe — ENTER only for tickers listed here) ==
{sector_map}

== PRICE SIGNALS ==
- Current price: Rs {price.get('current_price', 'N/A')}
- 30-day trend: {price.get('trend_30d_pct', 'N/A')}%
- RSI (14): {price.get('rsi_14', 'N/A')} -> {price.get('rsi_signal', 'N/A')}
- Volume: {price.get('volume_signal', 'N/A')}

== RECENT HEADLINES (multi-source) ==
{_format_headlines(news)}

== NEWS SENTIMENT (LLM-scored from headlines) ==
- Overall: {sentiment.get('sentiment', 'neutral')} (score: {sentiment.get('score', 0)})
- Summary: {sentiment.get('summary', 'No summary')}
- Key themes: {', '.join(sentiment.get('key_themes', []))}

== OFFICIAL PSX ANNOUNCEMENTS (last 30d) ==
{_format_announcements(announcements)}

Provide your decision JSON now.
"""


def _extract_first_json_object(text):
    """Parse the first top-level `{ ... }` value; ignores trailing model chatter."""
    if not text:
        return None
    dec = json.JSONDecoder()
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        try:
            obj, _end = dec.raw_decode(text, i)
            return obj
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            if i == -1:
                break
    return None


def _parse_decision(raw):
    """Accept strict JSON, fenced blocks, or JSON followed by extra prose."""
    s = (raw if raw is not None else "").strip()
    cleaned = s.replace("```json", "").replace("```", "").strip()

    for cand in (s, cleaned):
        if not cand:
            continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            obj = _extract_first_json_object(cand)
            if isinstance(obj, dict):
                return obj
    raise json.JSONDecodeError("Could not parse decision JSON", s or "", 0)


def gather_ticker_data(ticker, company_name):
    print(f"\n{'-'*50}\n  Gathering {ticker} ({company_name})\n{'-'*50}")
    print("  [1/4] price...")
    price = get_price_data(ticker)
    print("  [2/4] news...")
    news = get_news_headlines(company_name, max_results=8, ticker=ticker)
    n_ct = news.get("count") or 0
    srcs = news.get("sources_used") or []
    if n_ct == 0:
        hint = news.get("error") or "; ".join((news.get("errors") or [])[:2]) or "no matches"
        print(f"        -> 0 headlines ({hint})")
    else:
        print(f"        -> {n_ct} headlines via {', '.join(srcs) or 'unknown'}")
    print("  [3/4] PSX announcements + PDF extraction...")
    announcements = get_announcements(ticker, days=30, max_results=8, extract_pdfs=3)
    items = announcements.get("announcements") or []
    by_method = {"text-layer": 0, "vision": 0}
    for it in items:
        if (it.get("content") or "").strip():
            by_method[it.get("content_method", "text-layer")] = (
                by_method.get(it.get("content_method", "text-layer"), 0) + 1
            )
    print(
        f"        -> {len(items)} filings in last 30d, "
        f"{by_method['text-layer']} via text-layer, {by_method['vision']} via vision"
    )
    print("  [4/4] sentiment...")
    sentiment = analyse_sentiment(company_name, news.get("headlines", []))
    return {
        "company": company_name,
        "price": price,
        "news": news,
        "announcements": announcements,
        "sentiment": sentiment,
    }


def analyse_stock(ticker, data, profile, portfolio, system_prompt):
    company_name = data["company"]
    price = data["price"]
    sentiment = data["sentiment"]
    announcements = data.get("announcements") or {}

    print(f"\n  [decide] {ticker}...")
    user_prompt = build_user_prompt(
        ticker,
        company_name,
        price,
        sentiment,
        announcements,
        profile,
        portfolio,
        news=data.get("news"),
    )
    raw = complete(system_prompt, user_prompt, max_tokens=700)
    decision = _parse_decision(raw)

    decision["ticker"] = ticker
    decision["price"] = price.get("current_price")
    return decision


ACTION_EMOJI = {
    "ENTER": "🟢",
    "ADD": "🟢",
    "HOLD": "🟡",
    "TRIM": "🟠",
    "EXIT": "🔴",
    "MONITOR": "⚪",
}

BUY_ACTIONS = {"ENTER", "ADD"}
SELL_ACTIONS = {"TRIM", "EXIT"}


def _last_decision_meta(ticker):
    """Latest logged decision metadata for ticker, if any."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                """
                SELECT decision, timestamp, confidence
                FROM decisions
                WHERE ticker = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (ticker,),
            ).fetchone()
        if not row:
            return None
        ts = row["timestamp"] or ""
        when = None
        try:
            when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            when = None
        return {
            "decision": (row["decision"] or "").upper(),
            "timestamp": ts,
            "when": when,
            "confidence": float(row["confidence"] or 0),
        }
    except Exception:
        return None


def _days_since(dt):
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def _expected_edge_pct(decision, data):
    """Rough upside/downside edge estimate to avoid low-edge churn."""
    px = float((data.get("price") or {}).get("current_price") or 0)
    trend = float((data.get("price") or {}).get("trend_30d_pct") or 0)
    sent = float((data.get("sentiment") or {}).get("score") or 0)
    tp = decision.get("take_profit_pkr")
    sl = decision.get("stop_loss_pkr")

    # Prefer explicit target/stop if the model gave one.
    if px > 0 and tp is not None:
        try:
            return max(0.0, ((float(tp) - px) / px) * 100.0)
        except Exception:
            pass
    if px > 0 and sl is not None and (decision.get("action") or "") in SELL_ACTIONS:
        try:
            return max(0.0, ((px - float(sl)) / px) * 100.0)
        except Exception:
            pass

    # Fallback proxy: trend + sentiment scaled.
    return max(0.0, (0.45 * trend) + (4.0 * sent))


def _set_hold(r, reason):
    """Override a recommendation to HOLD with an audit trail."""
    original = (r.get("action") or "HOLD").upper()
    if original == "HOLD":
        return
    r["original_action"] = original
    r["action"] = "HOLD"
    r["shares"] = 0
    r["size_pkr"] = 0.0
    why = f"Policy gate: {reason}"
    if r.get("sizing_note"):
        r["sizing_note"] = f"{r.get('sizing_note')} | {why}"
    else:
        r["sizing_note"] = why
    base = (r.get("reasoning") or "").strip()
    if base:
        r["reasoning"] = f"{base} {why}."
    else:
        r["reasoning"] = why


def apply_trade_policy(results, ticker_data, portfolio, profile):
    """Reduce churn via confidence, edge-over-cost, and cooldown gates."""
    if not profile or not portfolio:
        return

    risk = profile.get("risk") or {}
    cap = profile.get("capital") or {}
    pref = profile.get("preferences") or {}

    min_conf_buy = float(pref.get("min_confidence_buy") or 0.75)
    min_conf_sell = float(pref.get("min_confidence_sell") or 0.70)
    min_edge_over_cost = float(pref.get("min_edge_over_cost_pct") or 1.0)
    core_cooldown_days = int(pref.get("core_cooldown_days") or 30)
    tactical_cooldown_days = int(pref.get("tactical_cooldown_days") or 7)
    rebalance_band = float(pref.get("rebalance_band_pct") or 2.0)

    brokerage = float(cap.get("brokerage_fee_pct") or 0.0)
    cgt = float(cap.get("cgt_pct") or 0.0)
    est_roundtrip_cost = (2.0 * brokerage) + (0.20 * cgt)

    held_tickers = {
        (h.get("ticker") or "").upper()
        for h in (profile.get("holdings") or [])
        if float(h.get("shares") or 0) > 0
    }
    tactical = {(t or "").upper() for t in (pref.get("tactical_tickers") or [])}
    core = {(t or "").upper() for t in (pref.get("core_tickers") or [])}
    if tactical:
        inferred_core = held_tickers - tactical
    else:
        inferred_core = held_tickers
    core |= inferred_core

    max_single = float(risk.get("max_single_position_pct") or 20.0)

    for r in results:
        act = (r.get("action") or "").upper()
        if act not in BUY_ACTIONS and act not in SELL_ACTIONS:
            continue
        t = (r.get("ticker") or "").upper()
        conf = float(r.get("confidence") or 0)
        data = ticker_data.get(t) or {}
        held = position_for(portfolio, t)
        wt = float(held.get("weight_pct") or 0) if held else 0.0
        over_cap = bool(held and wt > (max_single + rebalance_band))

        # Confidence gate
        if act in BUY_ACTIONS and conf < min_conf_buy:
            _set_hold(r, f"buy confidence {conf:.2f} < {min_conf_buy:.2f}")
            continue
        if act in SELL_ACTIONS and not over_cap and conf < min_conf_sell:
            _set_hold(r, f"sell confidence {conf:.2f} < {min_conf_sell:.2f}")
            continue

        # Edge-over-cost gate (main anti-churn filter)
        edge = _expected_edge_pct(r, data)
        hurdle = est_roundtrip_cost + min_edge_over_cost
        if act in BUY_ACTIONS and edge < hurdle:
            _set_hold(
                r,
                f"edge {edge:.2f}% below hurdle {hurdle:.2f}% (cost+buffer)",
            )
            continue

        # Cooldown gate (looser for tactical sleeve)
        last = _last_decision_meta(t)
        if last and last.get("when"):
            days = _days_since(last["when"])
            if days is not None:
                cooldown = tactical_cooldown_days if t in tactical else core_cooldown_days
                if days < cooldown and not over_cap:
                    _set_hold(
                        r,
                        f"cooldown active ({days:.1f}d < {cooldown}d) for {'tactical' if t in tactical else 'core'} sleeve",
                    )
                    continue

def _action_line(action, trade_shares, trade_size_pkr, held):
    """Return a single human-readable line describing the trade vs current position.

    Examples:
      ENTER   "Buy 12 sh ≈ Rs 6,480   →  hold 0 → 12 sh"
      ADD     "Buy 12 sh ≈ Rs 6,480   →  hold 48 → 60 sh"
      TRIM    "Sell 24 sh ≈ Rs 11,842  →  hold 48 → 24 sh"
      EXIT    "Sell all 48 sh ≈ Rs 23,672  →  hold 48 → 0 sh"
      HOLD    "Currently holding 75 sh @ avg Rs 178.83 (19.6% wt, P&L Rs -1,140)"
      HOLD*   "No position — no action"   (when held is None and HOLD)
      MONITOR "Watching — no position"
    """
    held_shares = int(held.get("shares", 0)) if held else 0

    if action in BUY_ACTIONS and trade_shares > 0:
        new_total = held_shares + trade_shares
        return (
            f"Buy {trade_shares} sh ≈ Rs {trade_size_pkr:,.0f}   →   "
            f"hold {held_shares} → {new_total} sh"
        )
    if action == "TRIM" and trade_shares > 0:
        new_total = max(0, held_shares - trade_shares)
        return (
            f"Sell {trade_shares} sh ≈ Rs {trade_size_pkr:,.0f}   →   "
            f"hold {held_shares} → {new_total} sh"
        )
    if action == "EXIT":
        sell_qty = trade_shares if trade_shares > 0 else held_shares
        return (
            f"Sell ALL {sell_qty} sh ≈ Rs {trade_size_pkr:,.0f}   →   "
            f"hold {held_shares} → 0 sh"
        )
    if action == "HOLD":
        if not held:
            return "No position — no action"
        return (
            f"Currently holding {held_shares} sh @ avg Rs {held.get('avg_cost_pkr')} "
            f"({held.get('weight_pct')}% wt, P&L Rs {held.get('pnl_pkr'):,.0f} "
            f"{held.get('pnl_pct'):+.2f}%)"
        )
    if action == "MONITOR":
        if held:
            return f"Watching · currently holding {held_shares} sh @ avg Rs {held.get('avg_cost_pkr')}"
        return "Watching — no position"
    if held_shares:
        return f"Holding {held_shares} sh @ avg Rs {held.get('avg_cost_pkr')}"
    return "No position"


VERDICT_SYSTEM = """You consolidate independent per-stock recommendations into ONE clear call for a human.
They do not want a menu of equal ENTERs — they want to know what to do first with available cash this week.
Be conservative. Output valid JSON only.
Do not mention model training cutoffs, "knowledge up to" dates, or years like 2023 — the user message already has live inputs for this run."""


PRIMARY_CONTINUITY_SYSTEM = """You compare the **last run's primary buy** with **this run's primary buy** for the same investor.
Both tickers include **fresh evidence from the current agent run** (same-day prices, RSI, reasoning). The prior run summary is metadata only (ticker, confidence, excerpt); judge strength mainly on today's snapshots.

Be direct and practical. No training-data or knowledge-cutoff language.

Return ONLY valid JSON:
{"lean": "prior" | "current" | "tie" | "either_ok" | "stand_pat" | "no_primary_this_run",
 "headline": "<one sentence>",
 "body_markdown": "<2-5 short paragraphs or bullets, markdown allowed, no code fences>"}

lean:
- prior = favor deploying / sticking with **last run's** ticker on today's evidence
- current = **this run's** primary is clearly the stronger buy now
- tie = similar conviction; user can choose on preference
- either_ok = both acceptable; no clear dominance
- stand_pat = wait or do neither on new cash
- no_primary_this_run = use when this run has no primary pick"""


def verdict_disclaimer():
    return (
        "Not financial advice; verify prices, fees, and your broker. "
        "Uses data fetched in this run (prices, news, filings), not a fixed AI knowledge cutoff."
    )


def _buy_candidates(results):
    out = []
    for r in results:
        if r.get("action") == "ERROR":
            continue
        act = r.get("action") or ""
        if act in ("ENTER", "ADD"):
            out.append(r)
    return out


def _sector_market_value(portfolio, sector, ticker_to_sector):
    if not portfolio or not sector:
        return 0.0
    t = 0.0
    for p in portfolio.get("positions") or []:
        if ticker_to_sector.get(p["ticker"]) == sector:
            t += float(p["market_value_pkr"] or 0)
    return t


def finalize_buy_sizes(results, verdict, portfolio, profile, ticker_to_sector):
    """Apply code-driven BUY sizing; only the portfolio primary uses deployable cash."""
    if not portfolio or not profile:
        for r in results:
            if r.get("action") in BUY_ACTIONS:
                r["shares"] = 0
                r["size_pkr"] = 0.0
                r["sizing_note"] = "Load profile.yaml for executable sizing."
                r["system_sizing"] = None
        return

    primary = verdict.get("primary_pick") or {}
    primary_ticker = primary.get("ticker")

    for r in results:
        act = r.get("action") or ""
        if act not in BUY_ACTIONS:
            continue
        ticker = r["ticker"]
        price = r.get("price") or 0
        held = position_for(portfolio, ticker)
        sector = ticker_to_sector.get(ticker)
        sector_mv = _sector_market_value(portfolio, sector, ticker_to_sector)
        alloc = compute_buy_allocation(
            portfolio, profile, act, price, held, sector_mv_pkr=sector_mv
        )

        if not primary_ticker:
            r["shares"] = 0
            r["size_pkr"] = 0.0
            r["sizing_note"] = (
                "No primary pick — do not deploy cash on ENTER/ADD this run."
            )
            r["system_sizing"] = None
            continue

        if ticker != primary_ticker:
            r["shares"] = 0
            r["size_pkr"] = 0.0
            r["sizing_note"] = (
                f"Signal only this cycle; cash allocated to primary **{primary_ticker}**."
            )
            r["system_sizing"] = None
            continue

        r["shares"] = alloc["shares"]
        r["size_pkr"] = alloc["size_pkr"]
        r["sizing_note"] = (
            "Sized from live cash + profile caps "
            f"(binding: {', '.join(alloc.get('capped_by') or [])})."
        )
        r["system_sizing"] = alloc

    if primary_ticker and primary:
        pr = next((x for x in results if x.get("ticker") == primary_ticker), None)
        if pr:
            primary["shares"] = int(pr.get("shares") or 0)
            primary["size_pkr"] = float(pr.get("size_pkr") or 0)
    verdict["markdown"] = _verdict_markdown(verdict)


def _build_signals_payload(data, decision, portfolio):
    held = position_for(portfolio, decision["ticker"])
    return {
        "price": data["price"],
        "news": data.get("news"),
        "sentiment": data["sentiment"],
        "announcements": data.get("announcements") or {},
        "action_details": {
            "action": decision.get("action"),
            "shares": int(decision.get("shares") or 0),
            "size_pkr": float(decision.get("size_pkr") or 0),
            "stop_loss_pkr": decision.get("stop_loss_pkr"),
            "take_profit_pkr": decision.get("take_profit_pkr"),
            "portfolio_impact": decision.get("portfolio_impact"),
            "time_horizon": decision.get("time_horizon"),
            "key_risks": decision.get("key_risks"),
            "held_at_decision": held,
            "system_sizing": decision.get("system_sizing"),
            "sizing_note": decision.get("sizing_note"),
        },
    }


def _run_snapshot_for_ticker(ticker, results, ticker_data, company_by_ticker):
    r = next((x for x in results if x.get("ticker") == ticker), None)
    data = ticker_data.get(ticker) or {}
    price = data.get("price") or {}
    name = company_by_ticker.get(ticker, ticker)
    if not r or r.get("action") == "ERROR":
        return f"### {ticker} ({name})\n_No decision this run._\n"
    ann = (data.get("announcements") or {}).get("announcements") or []
    ann_titles = [x.get("title", "")[:80] for x in ann[:2]]
    ann_line = "; ".join(ann_titles) if ann_titles else "—"
    return "\n".join(
        [
            f"### {ticker} ({name})",
            f"- Action: **{r.get('action')}**  confidence: {r.get('confidence')}",
            f"- Sized this run (if primary): {int(r.get('shares') or 0)} sh ≈ Rs {float(r.get('size_pkr') or 0):,.0f}",
            f"- Price: Rs {price.get('current_price')}  30d trend: {price.get('trend_30d_pct')}%  "
            f"RSI {price.get('rsi_14')} ({price.get('rsi_signal')})  vol: {price.get('volume_signal')}",
            f"- Top filings (titles): {ann_line}",
            f"- Reasoning excerpt: {(r.get('reasoning') or '')[:720]}",
        ]
    )


def _primary_continuity_fallback(prev_primary, verdict, results):
    pk = verdict.get("primary_pick") if verdict else None
    lines = [
        "## Primary continuity (last run vs this run)",
        "",
    ]
    if not prev_primary:
        lines.append("_No prior primary found in the database (first run or legacy data)._")
        if pk:
            t = pk.get("ticker")
            lines.append("")
            lines.append(
                f"This run's primary is **{t}** — see the portfolio verdict below for the sized order."
            )
        lines.append("")
        return "\n".join(lines)

    pt = prev_primary["ticker"]
    sz = float(prev_primary.get("size_pkr") or 0)
    lines.append(
        f"- **Last run primary:** **{pt}** ({prev_primary.get('action')}) "
        f"· conf {prev_primary.get('confidence')} "
        f"· was sized **{int(prev_primary.get('shares') or 0)} sh ≈ Rs {sz:,.0f}**"
    )
    if pk:
        ct = pk.get("ticker")
        csz = float(pk.get("size_pkr") or 0)
        lines.append(
            f"- **This run primary:** **{ct}** ({pk.get('action')}) "
            f"· **{int(pk.get('shares') or 0)} sh ≈ Rs {csz:,.0f}**"
        )
        rp = next((x for x in results if x.get("ticker") == pt), None)
        rc = next((x for x in results if x.get("ticker") == ct), None)
        cpp = float((rc or {}).get("confidence") or 0)
        ppp = float((rp or {}).get("confidence") or 0)
        if pt == ct:
            lines.append(
                "- **Heuristic:** Same ticker both runs — compare reasoning/RSI vs last time in the detail section."
            )
        elif cpp > ppp + 0.08:
            lines.append(
                "- **Heuristic:** Today's scores give **higher confidence** to this run's pick; "
                "review narrative unless you have a thesis for the prior name."
            )
        elif ppp > cpp + 0.08:
            lines.append(
                "- **Heuristic:** **Last run's** name still shows **higher confidence** on fresh data — "
                "consider sticking unless the new pick has a clearer catalyst."
            )
        else:
            lines.append(
                "- **Heuristic:** **Similar confidence** — choose from fundamentals, dividends, or sector limits; "
                "or run again with `PRIMARY_CONTINUITY_LLM=1` for a written comparison."
            )
    else:
        lines.append("- **This run primary:** None — no new consolidated buy vs last run.")
    lines.append("")
    return "\n".join(lines)


def synthesize_primary_continuity(prev_primary, verdict, results, ticker_data, company_by_ticker):
    """Compare last run's primary with this run's using today's snapshots for both names."""
    off = os.getenv("PRIMARY_CONTINUITY_LLM", "1").strip().lower()
    if off in ("0", "false", "no", "off"):
        return {
            "section_markdown": _primary_continuity_fallback(prev_primary, verdict, results),
            "lean": "heuristic",
            "headline": "",
        }

    pk = verdict.get("primary_pick") if verdict else None
    if not prev_primary and not pk:
        return {"section_markdown": "", "lean": "", "headline": ""}

    tickers_needed = set()
    if prev_primary and prev_primary.get("ticker"):
        tickers_needed.add(prev_primary["ticker"])
    if pk and pk.get("ticker"):
        tickers_needed.add(pk["ticker"])
    blocks = [_run_snapshot_for_ticker(t, results, ticker_data, company_by_ticker) for t in sorted(tickers_needed)]

    user = (
        "Last run primary (from database — metadata only):\n"
        f"{json.dumps(prev_primary, indent=2) if prev_primary else 'null'}\n\n"
        "This run primary (after sizing):\n"
        f"{json.dumps(pk, indent=2) if pk else 'null'}\n\n"
        "Fresh evidence from **this** run for each ticker:\n"
        f"{chr(10).join(blocks)}"
    )

    try:
        raw = complete(PRIMARY_CONTINUITY_SYSTEM, user, max_tokens=550)
        data = _parse_decision(raw)
        body = (data.get("body_markdown") or "").strip()
        headline = (data.get("headline") or "").strip()
        lean = (data.get("lean") or "unclear").strip()
        out_lines = ["## Primary continuity (last run vs this run)", ""]
        if headline:
            out_lines.append(f"**Quick take:** {headline}")
            out_lines.append("")
        if lean:
            out_lines.append(f"_Lean: **{lean}**_")
            out_lines.append("")
        if body:
            out_lines.append(body)
            out_lines.append("")
        return {
            "section_markdown": "\n".join(out_lines),
            "lean": lean,
            "headline": headline,
        }
    except Exception:
        return {
            "section_markdown": _primary_continuity_fallback(prev_primary, verdict, results),
            "lean": "fallback",
            "headline": "",
        }


def _format_primary_continuity_stdout(cont):
    if not cont:
        return
    h = cont.get("headline")
    lean = cont.get("lean")
    if h and lean:
        print(f"\n  Primary continuity [{lean}]: {h}\n")
    elif cont.get("section_markdown"):
        print("\n  Primary continuity: see report block (quick take in markdown).\n")


def _verdict_markdown(verdict: dict) -> str:
    """Markdown block inserted into the written report."""
    lines = [
        "## Portfolio verdict — if you only do one thing",
        "",
    ]
    p = verdict.get("primary_pick")
    defer_early = verdict.get("defer") or []
    if not p:
        lines.append(
            "**Primary:** None — do **not** treat multiple ENTER/ADD lines below as equal orders. "
            "No single name cleared the bar for “deploy cash first” this week."
        )
        lines.append("")
        if defer_early:
            lines.append("**Why ideas were not promoted to a single primary:**")
            lines.append("")
            for d in defer_early:
                lines.append(
                    f"- **{d.get('ticker')}** ({d.get('action', '')}): {d.get('one_line', '')}"
                )
            lines.append("")
        if verdict.get("note"):
            lines.append(verdict["note"])
            lines.append("")
        lines.append(
            "_Per-ticker rows are isolated scores; use this section as the tie-breaker._"
        )
        return "\n".join(lines) + "\n"

    t = p.get("ticker", "?")
    act = p.get("action", "?")
    sh = int(p.get("shares") or 0)
    sz = float(p.get("size_pkr") or 0)
    lines.append(
        f"**Do this first:** **{act} {t}** — {sh} sh ≈ Rs {sz:,.0f} PKR."
    )
    lines.append("")
    lines.append(f"**Why:** {p.get('one_line', '—')}")
    lines.append("")
    defer = verdict.get("defer") or []
    if defer:
        lines.append("**Defer for now** (still fine names — not the top priority this week):")
        lines.append("")
        for d in defer:
            lines.append(
                f"- **{d.get('ticker')}** ({d.get('action', '')}): {d.get('one_line', '')}"
            )
        lines.append("")
    if verdict.get("disclaimer"):
        lines.append(f"_{verdict['disclaimer']}_")
        lines.append("")
    lines.append(
        "_Each ticker was scored in isolation; this section is the tie-breaker so you are not choosing blind._"
    )
    return "\n".join(lines) + "\n"


def _format_verdict_stdout(verdict: dict) -> None:
    print(f"\n{'='*60}")
    print("  PORTFOLIO VERDICT — one primary action this week")
    print(f"{'='*60}\n")
    p = verdict.get("primary_pick")
    if not p:
        print("  Primary: NONE — no new buy recommended as first priority.")
        print("           Review HOLD/TRIM/EXIT rows and cash rules above.\n")
        if verdict.get("note"):
            print(f"  Note: {verdict['note']}\n")
        return
    t = p.get("ticker")
    act = p.get("action")
    sh = int(p.get("shares") or 0)
    sz = float(p.get("size_pkr") or 0)
    print(f"  ** Do first: {act} {t} — {sh} sh ≈ Rs {sz:,.0f}")
    print(f"     {p.get('one_line', '')}\n")
    for d in verdict.get("defer") or []:
        print(
            f"  Defer: {d.get('ticker')} ({d.get('action')}) — {d.get('one_line', '')}"
        )
    print()


def synthesize_portfolio_verdict(results, portfolio, profile, company_by_ticker):
    """Pick at most one primary ENTER/ADD when several tickers lit up."""
    buys = _buy_candidates(results)
    if not buys:
        note = None
        if profile:
            cap = profile.get("capital") or {}
            if float(cap.get("investable_cash_pkr") or 0) <= 0:
                note = "No investable cash in profile."
        return {
            "primary_pick": None,
            "defer": [],
            "markdown": _verdict_markdown({"primary_pick": None, "note": note}),
            "note": note,
        }

    if len(buys) == 1:
        b = buys[0]
        name = company_by_ticker.get(b["ticker"], b["ticker"])
        pk = {
            "ticker": b["ticker"],
            "action": b["action"],
            "shares": int(b.get("shares") or 0),
            "size_pkr": float(b.get("size_pkr") or 0),
            "one_line": (b.get("reasoning") or "")[:280]
            or f"Only buy candidate this run ({name}).",
        }
        vd = {"primary_pick": pk, "defer": [], "disclaimer": verdict_disclaimer()}
        vd["markdown"] = _verdict_markdown(vd)
        return vd

    # Multiple candidates — ask LLM to rank; fallback on failure
    goal = (profile or {}).get("goal") or {}
    risk = (profile or {}).get("risk") or {}
    cap = (profile or {}).get("capital") or {}
    cash = float(cap.get("investable_cash_pkr") or 0) if portfolio else 0
    if portfolio:
        cash = portfolio.get("cash_pkr", cash)

    rows = []
    for b in buys:
        sec = TICKER_SECTOR.get(b["ticker"], "")
        nm = company_by_ticker.get(b["ticker"], b["ticker"])
        rows.append(
            f"- {b['ticker']} ({nm}) [{sec}]: {b['action']} "
            f"{int(b.get('shares') or 0)} sh ≈ Rs {float(b.get('size_pkr') or 0):,.0f} | "
            f"conf {int((b.get('confidence') or 0) * 100)}% | "
            f"{(b.get('reasoning') or '')[:200]}"
        )
    table = "\n".join(rows)

    user = f"""Goal: {goal.get('style', 'unknown')} — {goal.get('notes', '')}
Risk: {risk.get('tolerance', 'medium')} | max single position {risk.get('max_single_position_pct', 20)}% | shariah: {risk.get('shariah_preference', 'none')}
Cash available for buys: Rs {cash:,.0f}

Buy candidates from per-ticker analysis:
{table}

Choose at most ONE primary_pick for "if I only execute one trade this week".
Every other ENTER/ADD above must appear in defer with a short one_line reason (not trashing the stock — just "rank #2 this week").
If none deserve capital now, primary_pick is null.

Return ONLY JSON:
{{"primary_pick": {{"ticker": "...", "action": "ENTER"|"ADD", "shares": <int>, "size_pkr": <float>, "one_line": "<2 sentences>"}} | null,
 "defer": [{{"ticker": "...", "action": "...", "one_line": "<short>"}}]}}"""

    try:
        raw = complete(VERDICT_SYSTEM, user, max_tokens=550)
        data = _parse_decision(raw)
        pk = data.get("primary_pick")
        defer = [d for d in (data.get("defer") or []) if isinstance(d, dict)]
        pk = pk if isinstance(pk, dict) else None
        if pk:
            chosen = pk.get("ticker")
            seen_t = {chosen} if chosen else set()
            for d in defer:
                if d.get("ticker"):
                    seen_t.add(d["ticker"])
            for b in buys:
                t = b["ticker"]
                if t not in seen_t:
                    defer.append(
                        {
                            "ticker": t,
                            "action": b.get("action"),
                            "one_line": "Lower priority vs primary this week.",
                        }
                    )
                    seen_t.add(t)
        else:
            # Arbiter said wait — ensure every buy candidate is explained in defer
            if not defer:
                for b in buys:
                    defer.append(
                        {
                            "ticker": b["ticker"],
                            "action": b.get("action"),
                            "one_line": "No consolidated primary — stand down or revisit next run.",
                        }
                    )
        vd = {
            "primary_pick": pk,
            "defer": defer,
            "disclaimer": verdict_disclaimer(),
        }
        vd["markdown"] = _verdict_markdown(vd)
        return vd
    except Exception:
        buys_sorted = sorted(
            buys, key=lambda r: -(float(r.get("confidence") or 0))
        )
        b0 = buys_sorted[0]
        pk = {
            "ticker": b0["ticker"],
            "action": b0["action"],
            "shares": int(b0.get("shares") or 0),
            "size_pkr": float(b0.get("size_pkr") or 0),
            "one_line": "Automated tie-break: highest confidence among buy candidates (verdict model unavailable).",
        }
        defer = []
        for b in buys_sorted[1:]:
            defer.append(
                {
                    "ticker": b["ticker"],
                    "action": b.get("action"),
                    "one_line": "Deferred by confidence tie-break.",
                }
            )
        vd = {
            "primary_pick": pk,
            "defer": defer,
            "disclaimer": verdict_disclaimer(),
        }
        vd["markdown"] = _verdict_markdown(vd)
        return vd


def print_report(results, portfolio):
    print(f"\n{'='*60}\n  PSX AGENT — PERSONAL REPORT\n{'='*60}\n")
    print(
        "  Each ticker is scored on its own; the **PORTFOLIO VERDICT** after this "
        "list picks at most **one** primary use of cash.\n"
    )
    if portfolio:
        print(
            f"  Equity Rs {portfolio['total_equity_pkr']:.0f} | "
            f"Cash Rs {portfolio['cash_pkr']:.0f} ({portfolio['cash_pct']:.1f}%) | "
            f"P&L Rs {portfolio['total_pnl_pkr']:.0f} ({portfolio['total_pnl_pct']:+.2f}%)"
        )
        if portfolio["breaches"]:
            print("  Breaches: " + "; ".join(portfolio["breaches"]))
        print()

    for r in results:
        action = r.get("action") or r.get("decision", "?")
        emoji = ACTION_EMOJI.get(action, "⚪")
        trade_shares = int(r.get("shares") or 0)
        trade_size = float(r.get("size_pkr") or 0)
        sl = r.get("stop_loss_pkr")
        tp = r.get("take_profit_pkr")
        conf = int((r.get("confidence") or 0) * 100)
        held = position_for(portfolio, r["ticker"])

        print(
            f"  {emoji}  {r['ticker']:<6}  {action:<7}  "
            f"conf {conf}%  px Rs {r.get('price','?')}"
        )
        print(f"       {_action_line(action, trade_shares, trade_size, held)}")
        if sl is not None or tp is not None:
            stop = f"Rs {sl}" if sl is not None else "—"
            tgt = f"Rs {tp}" if tp is not None else "—"
            print(f"       Stop: {stop}   |   Target: {tgt}")
        if r.get("reasoning"):
            print(f"       {r['reasoning']}")
        if r.get("portfolio_impact"):
            print(f"       Portfolio: {r['portfolio_impact']}")
        if r.get("key_risks"):
            print(f"       Risks: {', '.join(r['key_risks'])}")
        print(f"       ID: {str(r.get('decision_id',''))[:8]}...\n")


def main():
    init_db()
    reset_dps_session()
    prev_primary = get_last_completed_primary()
    profile = load_profile()
    if profile:
        print(f"  Loaded profile.yaml (goal={profile.get('goal',{}).get('style')}, "
              f"risk={profile.get('risk',{}).get('tolerance')})")
    else:
        print("  No profile.yaml found — running in generic mode.")

    tickers = dict(WATCHLIST)
    if profile:
        for h in profile.get("holdings") or []:
            t = h.get("ticker")
            if t and t not in tickers:
                tickers[t] = t

    ticker_data = {}
    for ticker, company_name in tickers.items():
        try:
            ticker_data[ticker] = gather_ticker_data(ticker, company_name)
        except Exception as e:
            print(f"  [!] gather failed on {ticker}: {e}")
            ticker_data[ticker] = {
                "company": company_name,
                "price": {"error": str(e), "ticker": ticker},
                "news": {"headlines": []},
                "sentiment": {"sentiment": "neutral", "score": 0},
            }

    price_by_ticker = {t: d["price"] for t, d in ticker_data.items()}
    apply_price_overrides(profile, price_by_ticker)
    portfolio = build_portfolio(profile, price_by_ticker)

    if portfolio:
        print(f"\n  Portfolio snapshot:")
        print(_portfolio_lines(portfolio))

    system_prompt = build_system_prompt(profile)

    results = []
    for ticker, data in ticker_data.items():
        try:
            results.append(analyse_stock(ticker, data, profile, portfolio, system_prompt))
        except Exception as e:
            print(f"  [!] decision failed on {ticker}: {e}")
            results.append({"ticker": ticker, "action": "ERROR", "reasoning": str(e)})

    apply_trade_policy(results, ticker_data, portfolio, profile)
    verdict = synthesize_portfolio_verdict(results, portfolio, profile, tickers)
    finalize_buy_sizes(results, verdict, portfolio, profile, TICKER_SECTOR)
    continuity = synthesize_primary_continuity(
        prev_primary, verdict, results, ticker_data, tickers
    )

    for r in results:
        if r.get("action") == "ERROR":
            continue
        try:
            data = ticker_data[r["ticker"]]
            signals = _build_signals_payload(data, r, portfolio)
            px = data["price"].get("current_price") or r.get("price") or 0
            decision_id = log_decision(
                ticker=r["ticker"],
                decision=r.get("action", "HOLD"),
                confidence=float(r.get("confidence", 0.5) or 0.5),
                signals=signals,
                reasoning=r.get("reasoning", "") or "",
                price_at_decision=px,
            )
            r["decision_id"] = decision_id
        except Exception as e:
            print(f"  [!] log failed on {r.get('ticker')}: {e}")

    _format_primary_continuity_stdout(continuity)
    _format_verdict_stdout(verdict)
    print_report(results, portfolio)

    print("  All decisions saved to db.sqlite")
    try:
        from report import persist_verdict_inject, write_reports

        vmd = verdict.get("markdown") or ""
        top = (continuity.get("section_markdown") or "").strip()
        verdict_block = top + "\n\n---\n\n" + vmd if top else vmd
        persist_verdict_inject(verdict_block)
        write_reports(verdict_section=verdict_block)
    except Exception as e:
        print(f"  [!] could not refresh reports/: {e}")
    print()


if __name__ == "__main__":
    main()
