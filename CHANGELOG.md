# Changelog

All notable parameter and prompt changes are recorded here so the impact on decisions can be tracked.
Each entry notes the date it went live; compare `data/decisions.json` before and after for empirical evaluation.

---

## v1.1.0 — 2026-05-30

### Watchlist
- **Removed** NESTLE (Nestle Pakistan) — high-float FMCG with thin liquidity and minimal dividend yield; poor fit for income strategy
- **Removed** INDU (Indus Motor Company) — auto sector highly cyclical, Shariah-preference conflict, dominated by news noise
- **Added** MCB (MCB Bank Limited) — strong conventional bank, consistent dividend payer; diversifies banking sleeve beyond Islamic
- **Added** SEARL (The Searle Company) — pharma sector with defensive characteristics, underrepresented in watchlist
- **Added** ABOT (Abbott Laboratories Pakistan) — established pharma, historically high dividend yields

### Price data
- **Extended yfinance history window from 3 months to 1 year** — RSI and trend now computed over a larger sample reducing short-term noise
- **Added 52-week high/low and range position %** — shows where current price sits within the annual range; gives context on mean-reversion potential

### Position sizing
- **Volume gate added to `compute_buy_allocation`** — when `volume_signal == "low"`, maximum position size is capped at 50% of the computed allocation. Low volume increases execution risk (wider spreads, partial fills); this prevents oversizing into thin markets.

### Trade policy
- **Core cooldown reduced 30 → 14 days** — 30-day cooldown was blocking re-entry after earnings events and dividend announcements that change the investment case materially. 14 days preserves anti-churn behaviour while allowing timely re-evaluation.

### LLM prompt / decision logic
- **Sentiment weight made explicit: ~20% only** — historically the agent over-weighted negative sentiment from headlines with no fundamental backing. PSX filings are now explicitly stated as primary evidence; sentiment is labelled a weak signal.
- **RSI neutral re-qualified** — RSI in neutral range (35–70) is no longer treated as a veto on ENTER. For income investors, RSI neutral + dividend yield ≥ 3% + positive/neutral filings is explicitly listed as a valid ENTER condition.
- **Income ENTER yield gate** — for income-goal portfolios, new ENTER positions require estimated forward dividend yield ≥ 3% unless a capital-gain catalyst is clearly evidenced in filings.
- **Cost basis recovery shown for underwater holdings** — the prompt now surfaces "needs +X% to recover cost basis" for positions with unrealised losses so the LLM can factor in the recovery required before selling.
- **Execution awareness** — last 3 broker executions per ticker are injected into the prompt so the LLM knows recent trade history and avoids redundant or whipsaw recommendations.
- **52-week range context in prompt** — position within 52w range is surfaced alongside price signals.
- **Sector portfolio weights in prompt** — current sector allocation % is shown alongside the ticker's sector so the LLM can reason about concentration risk per sector, not just per name.

### Database
- **New `agent_changes` table** — parameter changes are now recorded in `db.sqlite` via `logger.log_agent_change()` so change history is queryable alongside decisions.

---

## v1.0.0 — pre-2026-05-30

Initial production version. 19 tickers, 3-month price window, 30-day cooldown.
