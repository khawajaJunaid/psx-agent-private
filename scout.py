"""
PSX Agent — Scout / pre-flight check.

A cheap daily check that decides whether running the full `agent.py` is warranted.
It does NOT score every ticker. It looks at:

  - KSE100 daily move
  - Recent market-wide headlines (last 24h)
  - Headlines for each ticker the user holds
  - Hours since the last full agent run (from db.sqlite)

It then asks the LLM for a single verdict:

  SKIP    — nothing material changed; do not bother running today
  RUN     — regular cadence justifies a run (weekly/monthly)
  URGENT  — material event for the user's portfolio; run after dust settles

Usage:
  python3 scout.py                      # just print verdict
  python3 scout.py --auto               # auto-run agent.py if verdict is URGENT or RUN
  python3 scout.py --auto-urgent-only   # auto-run only on URGENT
  python3 scout.py --json               # machine-readable JSON only

Env knobs:
  SCOUT_LOOKBACK_HOURS=48               # how recent "recent news" means (default 48)
  SCOUT_HEADLINES_PER_TICKER=4          # cap per ticker
  SCOUT_FORCE_RUN_AFTER_HOURS=168       # 7 days: scout starts leaning RUN
  SCOUT_SHOCK_HEADLINE_KEYWORDS=war,attack,coup,default,curfew,blast,strike,...
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from tools.llm import complete
from tools.news import get_news_headlines
from tools.kse100 import fetch_kse100_snapshot
from tools.profile import load_profile, holdings_tickers
from logger import get_conn, DB_PATH

load_dotenv()


# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

DEFAULT_SHOCK_KEYWORDS = [
    "war", "strike", "attack", "missile", "drone", "blast", "explosion",
    "curfew", "martial", "coup", "regime", "default", "imf",
    "devalu", "rupee crash", "currency crisis", "shutdown",
    "circuit breaker", "trading halt", "suspension", "halted",
    "emergency", "lockdown", "border", "ceasefire",
    "downgrade", "sovereign", "crash", "plunge",
]


SCOUT_SYSTEM_PROMPT = """
You are a CONSERVATIVE market-monitoring scout for a personal stock-analysis
agent. The user actively manages their portfolio and typically runs a full
analysis 2-3 times per week. Most days without material news the answer is SKIP.

You receive a JSON snapshot containing:
  - KSE100 last value, daily change, daily percentage move (live, today)
  - Hours since the last full agent run
  - Market-wide headlines from the last <= max_age_hours hours, EACH WITH AN AGE IN HOURS
  - Per-holding headlines (also dated)
  - The user's holdings, goal, and risk profile

You may ONLY treat a headline as "recent" if its `age_hours` is <= 72.
Stale, undated, or older headlines must be IGNORED.

Decide ONE verdict:
  SKIP   : nothing material since last run; daily noise only.
  RUN    : regular cadence justifies a run (>= 3 days since last) OR mild macro shifts OR light per-holding news.
  URGENT : there is HARD CONFIRMED evidence — typically requires BOTH:
           (a) at least 2 recent (age_hours <= 48) shock-type headlines, AND
           (b) KSE100 |daily_pct| >= 2%, OR per-holding material news.

Hard rules — escalate to URGENT ONLY if at least one of these is true:
  1. KSE100 |daily_pct| >= 3% AND at least 1 recent shock headline.
  2. >= 2 recent (<= 48h) headlines naming material per-holding events
     (earnings, dividend, M&A, regulator action, suspension, large profit warning).
  3. >= 2 recent (<= 48h) headlines about war / IMF default / currency crisis
     / coup / curfew / large index drop, AND KSE100 |daily_pct| >= 2%.

NEVER treat any of the following as URGENT triggers on their own:
  - A single old / undated "plunge" or "border" headline
  - Generic "PSX opens cautious / mixed / flat" headlines
  - Opinion pieces, rumors, social media chatter
  - Just hours-since-last-run by itself (that's RUN, not URGENT)

If your reasons cite headlines, you MUST quote their `age_hours` so the user
can audit your decision.

Respond ONLY with this JSON (no markdown, no prose outside JSON):
{
  "verdict": "SKIP" | "RUN" | "URGENT",
  "confidence": <0.0-1.0>,
  "headline": "<one short sentence, <= 18 words>",
  "reasons": ["<3-5 bullets, each citing concrete evidence and age_hours where relevant>"],
  "next_check_hours": <integer estimate>,
  "shock_mode_recommended": <true|false>,
  "watch_tickers": ["<tickers worth a closer look in next run>"]
}
""".strip()


# ----------------------------------------------------------------------------
# Data gathering
# ----------------------------------------------------------------------------

def _kse100_snapshot():
    """Live KSE100 from PSX DPS (with yfinance fallback)."""
    return fetch_kse100_snapshot()


def _hours_since_last_run():
    """Hours since the most recent decision in db.sqlite (any ticker)."""
    if not DB_PATH.exists():
        return None
    try:
        with get_conn() as conn:
            row = conn.execute("SELECT MAX(timestamp) FROM decisions").fetchone()
        if not row or not row[0]:
            return None
        last = datetime.fromisoformat(row[0])
        return round((datetime.utcnow() - last).total_seconds() / 3600.0, 1)
    except Exception:
        return None


def _fetch_one_ticker(profile, ticker, per_ticker_cap, max_age_hours):
    try:
        holding = next(
            (h for h in (profile.get("holdings") or []) if h.get("ticker") == ticker),
            None,
        )
        company = (holding or {}).get("company") or _company_for(ticker)
        res = get_news_headlines(
            company or ticker,
            max_results=per_ticker_cap,
            ticker=ticker,
            max_age_hours=max_age_hours,
        )
        items = res.get("items", []) if isinstance(res, dict) else []
        return ticker, [
            {
                "title": it["title"],
                "age_hours": it.get("age_hours"),
                "published_utc": it.get("published_utc"),
            }
            for it in items[:per_ticker_cap]
        ]
    except Exception as e:
        return ticker, [{"title": f"(news fetch failed: {e})", "age_hours": None}]


def _holding_headlines(profile, per_ticker_cap, max_age_hours):
    """Return {ticker: [{title, age_hours, published_utc}, ...]}.

    Fetches each ticker's news in parallel to keep the scout fast.
    """
    out: dict = {}
    if not profile:
        return out
    tickers = holdings_tickers(profile)
    if not tickers:
        return out
    with ThreadPoolExecutor(max_workers=min(8, len(tickers))) as pool:
        futures = [
            pool.submit(_fetch_one_ticker, profile, t, per_ticker_cap, max_age_hours)
            for t in tickers
        ]
        for fut in futures:
            ticker, items = fut.result()
            out[ticker] = items
    return out


def _company_for(ticker):
    try:
        from watchlist import WATCHLIST
        return WATCHLIST.get(ticker, ticker)
    except Exception:
        return ticker


def _fetch_one_query(q, per_query_cap, max_age_hours):
    try:
        res = get_news_headlines(q, max_results=per_query_cap, max_age_hours=max_age_hours)
        items = res.get("items", []) if isinstance(res, dict) else []
        return [
            {
                "title": it.get("title") or "",
                "age_hours": it.get("age_hours"),
                "published_utc": it.get("published_utc"),
            }
            for it in items
        ]
    except Exception:
        return []


def _market_headlines(per_query_cap=6, max_age_hours=72):
    queries = [
        "Pakistan Stock Exchange",
        "KSE100 today",
        "State Bank of Pakistan",
        "PKR USD exchange",
    ]
    seen = set()
    aggregated = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda q: _fetch_one_query(q, per_query_cap, max_age_hours),
            queries,
        ))
    for items in results:
        for it in items:
            title = (it.get("title") or "").strip()
            k = title.lower()
            if not k or k in seen:
                continue
            seen.add(k)
            aggregated.append(it)
            if len(aggregated) >= 16:
                break
        if len(aggregated) >= 16:
            break
    return aggregated[:16]


def _shock_keywords():
    raw = os.getenv("SCOUT_SHOCK_HEADLINE_KEYWORDS", "").strip()
    if raw:
        return [k.strip().lower() for k in raw.split(",") if k.strip()]
    return DEFAULT_SHOCK_KEYWORDS


def _shock_hits(headline_items, keywords, max_age_hours=48):
    """Return shock-keyword hits for items that are dated AND within max_age_hours."""
    hits = []
    for item in headline_items:
        if isinstance(item, dict):
            title = item.get("title") or ""
            age = item.get("age_hours")
        else:
            title = str(item or "")
            age = None
        if age is None:
            continue
        if age > max_age_hours:
            continue
        low = title.lower()
        for k in keywords:
            if k in low:
                hits.append({"headline": title, "keyword": k, "age_hours": age})
                break
    return hits


# ----------------------------------------------------------------------------
# DB persistence (optional, lightweight)
# ----------------------------------------------------------------------------

def _ensure_scout_table():
    if not DB_PATH.exists():
        return
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scout_runs (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                verdict TEXT NOT NULL,
                confidence REAL,
                headline TEXT,
                reasons TEXT,
                kse100 TEXT,
                hours_since_last_run REAL,
                shock_hits TEXT,
                payload TEXT
            )
            """
        )
        conn.commit()


REPORTS_DIR = Path(__file__).parent / "reports"


VERDICT_LONG = {
    "SKIP": "⚪ SKIP — nothing material today; close the laptop.",
    "RUN": "🟢 RUN — regular cadence justifies a full agent pass.",
    "URGENT": "🔴 URGENT — material event detected; cool 24-48h, then run with shock mode.",
}


def _md_kse_line(kse):
    if not isinstance(kse, dict) or kse.get("last") is None:
        return f"- **KSE100:** _unavailable_ (`{kse}`)"
    last = kse["last"]
    ch = kse.get("daily_change")
    pct = kse.get("daily_pct")
    ch_s = f"{ch:+,.2f}" if isinstance(ch, (int, float)) else "?"
    pct_s = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "?"
    return f"- **KSE100:** {last:,.2f} · today {ch_s} ({pct_s}) · source `{kse.get('source','?')}`"


def _build_markdown(verdict, kse, hours_ago, shock_hits, market_news, holding_news, payload):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    v = verdict.get("verdict", "SKIP")
    conf = int((verdict.get("confidence") or 0) * 100)
    lines = [
        f"# PSX Scout — pre-flight check",
        "",
        f"_Generated {now} (local)_",
        "",
        "## Verdict",
        "",
        f"### {VERDICT_LONG.get(v, v)}",
        "",
        f"- **Confidence:** {conf}%",
    ]
    if verdict.get("headline"):
        lines.append(f"- **Quick take:** {verdict['headline']}")
    if verdict.get("next_check_hours") is not None:
        lines.append(f"- **Next suggested check:** in ~{verdict['next_check_hours']}h")
    if verdict.get("shock_mode_recommended"):
        lines.append(f"- **Shock mode:** recommended (`PSX_SHOCK_MODE=1 python3 agent.py`)")
    if verdict.get("watch_tickers"):
        lines.append(f"- **Watch:** {', '.join(verdict['watch_tickers'])}")
    lines.append("")

    lines.extend(["## Snapshot", "", _md_kse_line(kse)])
    lines.append(f"- **Hours since last full agent run:** {hours_ago}")
    lines.append(f"- **Recent shock-keyword hits (≤48h):** {len(shock_hits or [])}")
    lines.append("")

    if verdict.get("reasons"):
        lines.extend(["## Reasons", ""])
        for r in verdict["reasons"]:
            lines.append(f"- {r}")
        lines.append("")

    if shock_hits:
        lines.extend(["## Recent shock-keyword evidence", "",
                      "| Age | Keyword | Headline |",
                      "|----:|---------|----------|"])
        for h in shock_hits[:10]:
            lines.append(f"| {h.get('age_hours','?')}h | `{h.get('keyword','')}` | {h.get('headline','')[:140]} |")
        lines.append("")

    if market_news:
        lines.extend(["## Market headlines (last 72h, dated only)", "",
                      "| Age | Headline |",
                      "|----:|----------|"])
        for it in market_news[:12]:
            age = it.get("age_hours")
            tag = f"{age}h" if age is not None else "?"
            lines.append(f"| {tag} | {it.get('title','')[:160]} |")
        lines.append("")

    if holding_news:
        lines.extend(["## Per-holding headlines (last 72h, dated only)", ""])
        for ticker, items in holding_news.items():
            lines.append(f"### {ticker}")
            lines.append("")
            if not items:
                lines.append("_No recent dated headlines._")
                lines.append("")
                continue
            lines.append("| Age | Headline |")
            lines.append("|----:|----------|")
            for it in items[:6]:
                age = it.get("age_hours")
                tag = f"{age}h" if age is not None else "?"
                lines.append(f"| {tag} | {it.get('title','')[:160]} |")
            lines.append("")

    lines.extend([
        "---",
        "",
        "## How to read this",
        "",
        "- **SKIP** → don't run the agent today. Close the tab.",
        "- **RUN** → regular cadence; run `python3 agent.py` at your convenience.",
        "- **URGENT** → wait 24-48h for the dust to settle, then `PSX_SHOCK_MODE=1 python3 agent.py`.",
        "",
        "_Cross-check the headlines above against your own source (Dawn, Business Recorder) if URGENT._",
        "",
    ])
    return "\n".join(lines)


def _write_scout_reports(verdict, kse, hours_ago, shock_hits, market_news, holding_news, payload):
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        body = _build_markdown(verdict, kse, hours_ago, shock_hits, market_news, holding_news, payload)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archived = REPORTS_DIR / f"scout-{stamp}.md"
        latest = REPORTS_DIR / "SCOUT_LATEST.md"
        archived.write_text(body, encoding="utf-8")
        latest.write_text(body, encoding="utf-8")
        return latest, archived
    except Exception:
        return None, None


def _log_scout(verdict_obj, kse, hours_ago, shock_hits, payload):
    if not DB_PATH.exists():
        return None
    try:
        _ensure_scout_table()
        rid = str(uuid.uuid4())
        with get_conn() as conn:
            conn.execute(
                """
                INSERT INTO scout_runs
                  (id, timestamp, verdict, confidence, headline, reasons,
                   kse100, hours_since_last_run, shock_hits, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    datetime.utcnow().isoformat(),
                    verdict_obj.get("verdict", "SKIP"),
                    verdict_obj.get("confidence"),
                    verdict_obj.get("headline"),
                    json.dumps(verdict_obj.get("reasons") or []),
                    json.dumps(kse or {}),
                    hours_ago,
                    json.dumps(shock_hits or []),
                    json.dumps(payload or {}),
                ),
            )
            conn.commit()
        return rid
    except Exception:
        return None


# ----------------------------------------------------------------------------
# LLM verdict
# ----------------------------------------------------------------------------

def _parse_json_loose(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned)


def _heuristic_fallback(kse, hours_ago, shock_hits, holding_news, force_after_hours):
    """Conservative heuristic when LLM is off / failed.

    URGENT requires: >= 2 recent (<= 48h) shock hits AND |KSE100 daily| >= 2%.
    RUN     fires on: time elapsed >= force_after_hours, OR |KSE100 daily| >= 3%
            with >= 1 recent shock hit, OR >= 1 dated per-holding shock hit.
    Otherwise: SKIP.
    """
    daily = None
    if isinstance(kse, dict):
        daily = kse.get("daily_pct")

    n_shock = len(shock_hits or [])
    abs_daily = abs(daily) if isinstance(daily, (int, float)) else None

    if n_shock >= 2 and abs_daily is not None and abs_daily >= 2.0:
        return {
            "verdict": "URGENT",
            "confidence": 0.75,
            "headline": (
                f"{n_shock} recent shock headlines + KSE100 {daily:+.2f}% today."
            ),
            "reasons": [
                f"{h['keyword']}: '{h['headline'][:80]}' ({h['age_hours']}h old)"
                for h in shock_hits[:4]
            ] + [f"KSE100 daily move {daily:+.2f}%"],
            "next_check_hours": 24,
            "shock_mode_recommended": abs_daily >= 3.0,
            "watch_tickers": [],
        }

    if abs_daily is not None and abs_daily >= 3.0 and n_shock >= 1:
        return {
            "verdict": "URGENT" if abs_daily >= 5.0 else "RUN",
            "confidence": 0.7,
            "headline": f"KSE100 moved {daily:+.2f}% today; one recent shock headline.",
            "reasons": [
                f"KSE100 daily move {daily:+.2f}% exceeds 3% threshold",
                *[f"{h['keyword']}: '{h['headline'][:80]}' ({h['age_hours']}h old)" for h in shock_hits[:2]],
            ],
            "next_check_hours": 24,
            "shock_mode_recommended": abs_daily >= 5.0,
            "watch_tickers": [],
        }

    if hours_ago is None or hours_ago >= force_after_hours:
        return {
            "verdict": "RUN",
            "confidence": 0.6,
            "headline": "It's been a while since the last full run.",
            "reasons": [
                f"hours since last run: {hours_ago}",
                f"force-run threshold: {force_after_hours}h",
            ],
            "next_check_hours": 24,
            "shock_mode_recommended": False,
            "watch_tickers": [],
        }

    return {
        "verdict": "SKIP",
        "confidence": 0.6,
        "headline": "No material change since last run.",
        "reasons": [
            f"KSE100 daily move {daily}",
            f"hours since last run: {hours_ago}",
            f"recent shock-keyword hits: {n_shock}",
        ],
        "next_check_hours": 24,
        "shock_mode_recommended": False,
        "watch_tickers": [],
    }


def _ask_llm(payload):
    user_prompt = (
        f"SNAPSHOT (UTC {datetime.utcnow().isoformat(timespec='minutes')}):\n"
        f"{json.dumps(payload, indent=2)}\n\n"
        f"Decide whether to run the full psx-agent now. Be conservative."
    )
    try:
        raw = complete(SCOUT_SYSTEM_PROMPT, user_prompt, max_tokens=400)
        return _parse_json_loose(raw)
    except Exception as e:
        return {"_error": str(e)}


def _normalize_verdict(verdict, hours_ago, force_after_hours, min_hours_between_run=12.0):
    """Post-process LLM verdicts into sane operational behavior.

    - Prevent repeated RUN calls shortly after a full run unless verdict is URGENT.
    - Clamp next_check_hours to a practical range.
    """
    v = dict(verdict or {})
    label = (v.get("verdict") or "SKIP").upper()
    if label not in {"SKIP", "RUN", "URGENT"}:
        label = "SKIP"
    v["verdict"] = label

    # If we just ran recently, default to SKIP unless explicitly URGENT.
    if (
        label == "RUN"
        and isinstance(hours_ago, (int, float))
        and hours_ago < float(min_hours_between_run)
    ):
        remain = max(1, int(round(float(min_hours_between_run) - float(hours_ago))))
        v["verdict"] = "SKIP"
        v["headline"] = "Recent full run already completed; waiting unless conditions turn urgent."
        rs = list(v.get("reasons") or [])
        rs.insert(0, f"hours since last run ({hours_ago:.1f}) < minimum rerun window ({min_hours_between_run:.1f}h)")
        v["reasons"] = rs
        v["next_check_hours"] = remain

    # Keep next-check recommendation practical.
    nh = v.get("next_check_hours")
    try:
        nh_i = int(float(nh))
    except Exception:
        nh_i = None
    if nh_i is None or nh_i <= 0:
        v["next_check_hours"] = 24 if v["verdict"] != "URGENT" else 6
    else:
        if v["verdict"] == "URGENT":
            v["next_check_hours"] = min(max(nh_i, 1), 24)
        else:
            v["next_check_hours"] = min(max(nh_i, 4), 72)
    return v


# ----------------------------------------------------------------------------
# Output formatting
# ----------------------------------------------------------------------------

VERDICT_EMOJI = {"SKIP": "⚪", "RUN": "🟢", "URGENT": "🔴"}


def _print_human(verdict, kse, hours_ago, shock_hits, holding_news, market_news, args):
    emoji = VERDICT_EMOJI.get(verdict.get("verdict", "SKIP"), "⚪")
    conf = int((verdict.get("confidence") or 0) * 100)
    print(f"\n{'='*60}")
    print(f"  PSX SCOUT — pre-flight check")
    print(f"{'='*60}")
    print(f"  {emoji}  Verdict: {verdict.get('verdict','SKIP')}   confidence {conf}%")
    if verdict.get("headline"):
        print(f"  {verdict['headline']}")
    print()
    if isinstance(kse, dict) and kse.get("last") is not None:
        last = kse["last"]
        ch = kse.get("daily_change")
        pct = kse.get("daily_pct")
        ch_s = f"{ch:+,.2f}" if isinstance(ch, (int, float)) else "?"
        pct_s = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "?"
        print(f"  KSE100: {last:,.2f}   today {ch_s} ({pct_s})   src={kse.get('source','?')}")
    else:
        print(f"  KSE100: {kse}")
    print(f"  Hours since last full agent run: {hours_ago}")
    if shock_hits:
        print(f"  Recent shock-keyword hits (<= 48h): {len(shock_hits)}")
        for h in shock_hits[:5]:
            print(f"    - [{h['keyword']}] {h['age_hours']}h ago: {h['headline'][:120]}")
    if verdict.get("watch_tickers"):
        print(f"  Watch: {', '.join(verdict['watch_tickers'])}")
    if verdict.get("reasons"):
        print(f"  Reasons:")
        for r in verdict["reasons"]:
            print(f"    - {r}")
    nh = verdict.get("next_check_hours")
    if nh is not None:
        print(f"  Next suggested check: in ~{nh}h")
    if verdict.get("shock_mode_recommended"):
        print(f"  Recommended: PSX_SHOCK_MODE=1 python3 agent.py")
    print()
    if args.verbose:
        print("  Market headlines (sample, last 72h, dated only):")
        if not market_news:
            print("    (none — feeds returned no recent dated items)")
        for it in market_news[:8]:
            age = it.get("age_hours")
            tag = f"({age}h)" if age is not None else "(?h)"
            print(f"    - {tag} {it['title'][:120]}")
        print()
        if holding_news:
            print("  Per-holding headlines (sample, last 72h, dated only):")
            for t, items in holding_news.items():
                print(f"    {t}:")
                if not items:
                    print(f"      (no recent dated headlines)")
                for it in items[:3]:
                    age = it.get("age_hours")
                    tag = f"({age}h)" if age is not None else "(?h)"
                    print(f"      - {tag} {it['title'][:120]}")
            print()


def _maybe_autorun(verdict_str, args, shock_recommended):
    """Optionally invoke `python3 agent.py` based on verdict."""
    should = False
    if args.auto and verdict_str in {"RUN", "URGENT"}:
        should = True
    if args.auto_urgent_only and verdict_str == "URGENT":
        should = True
    if not should:
        return None

    env = os.environ.copy()
    if shock_recommended:
        env["PSX_SHOCK_MODE"] = "1"
    print(f"  >> Auto-running agent.py (PSX_SHOCK_MODE={env.get('PSX_SHOCK_MODE','0')})...\n")
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "agent.py")],
            env=env,
            check=False,
        )
        return proc.returncode
    except Exception as e:
        print(f"  [!] auto-run failed: {e}")
        return None


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="PSX Agent scout / pre-flight check")
    parser.add_argument("--auto", action="store_true", help="Auto-run agent.py on RUN or URGENT")
    parser.add_argument("--auto-urgent-only", action="store_true", help="Auto-run agent.py only on URGENT")
    parser.add_argument("--json", action="store_true", help="Print verdict as JSON only")
    parser.add_argument("--verbose", action="store_true", help="Show fetched headlines")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM call; use heuristic only")
    parser.add_argument("--no-report", action="store_true", help="Don't write reports/SCOUT_LATEST.md")
    args = parser.parse_args()

    profile = load_profile()
    per_ticker_cap = int(os.getenv("SCOUT_HEADLINES_PER_TICKER", "4"))
    force_after_hours = float(os.getenv("SCOUT_FORCE_RUN_AFTER_HOURS", "72"))
    max_age_hours = float(os.getenv("SCOUT_LOOKBACK_HOURS", "72"))
    shock_window = float(os.getenv("SCOUT_SHOCK_WINDOW_HOURS", "48"))
    min_hours_between_run = float(os.getenv("SCOUT_MIN_HOURS_BETWEEN_RUN", "24"))

    if not args.json:
        print("  [scout] fetching KSE100 (PSX DPS)...")
    kse = _kse100_snapshot()

    if not args.json:
        print("  [scout] checking last run...")
    hours_ago = _hours_since_last_run()

    if not args.json:
        print(f"  [scout] fetching market headlines (last {int(max_age_hours)}h)...")
    market_news = _market_headlines(max_age_hours=max_age_hours)

    if not args.json:
        print(f"  [scout] fetching per-holding headlines (last {int(max_age_hours)}h)...")
    holding_news = _holding_headlines(profile, per_ticker_cap, max_age_hours)

    keywords = _shock_keywords()
    all_items = list(market_news)
    for items in holding_news.values():
        all_items.extend(items)
    shock_hits = _shock_hits(all_items, keywords, max_age_hours=shock_window)

    payload = {
        "kse100": kse,
        "hours_since_last_run": hours_ago,
        "force_run_after_hours": force_after_hours,
        "max_age_hours": max_age_hours,
        "shock_window_hours": shock_window,
        "recent_shock_hits": shock_hits[:10],
        "holdings": holdings_tickers(profile) if profile else [],
        "risk": (profile or {}).get("risk", {}),
        "goal": (profile or {}).get("goal", {}),
        "market_headlines": market_news,
        "holding_headlines": holding_news,
    }

    if args.no_llm:
        verdict = _heuristic_fallback(kse, hours_ago, shock_hits, holding_news, force_after_hours)
    else:
        verdict = _ask_llm(payload)
        if not verdict or verdict.get("_error") or "verdict" not in verdict:
            if not args.json:
                print(f"  [scout] LLM call failed or unparseable; using heuristic. ({verdict.get('_error', '')})")
            verdict = _heuristic_fallback(kse, hours_ago, shock_hits, holding_news, force_after_hours)
    verdict = _normalize_verdict(
        verdict,
        hours_ago=hours_ago,
        force_after_hours=force_after_hours,
        min_hours_between_run=min_hours_between_run,
    )

    _log_scout(verdict, kse, hours_ago, shock_hits, payload)

    report_paths = (None, None)
    if not args.no_report:
        report_paths = _write_scout_reports(
            verdict, kse, hours_ago, shock_hits, market_news, holding_news, payload
        )

    if args.json:
        out = {
            "verdict": verdict,
            "kse100": kse,
            "hours_since_last_run": hours_ago,
            "shock_hits_count": len(shock_hits),
        }
        if report_paths[0]:
            out["report_latest"] = str(report_paths[0])
            out["report_archive"] = str(report_paths[1])
        print(json.dumps(out, indent=2))
    else:
        _print_human(verdict, kse, hours_ago, shock_hits, holding_news, market_news, args)
        if report_paths[0]:
            print(f"  Report: {report_paths[0]}")
            print(f"  Archive: {report_paths[1]}\n")

    rc = _maybe_autorun(verdict.get("verdict", "SKIP"), args, bool(verdict.get("shock_mode_recommended")))
    if rc is not None:
        sys.exit(rc)


if __name__ == "__main__":
    main()
