# Agent Architecture — psx-agent

## Overview

psx-agent is a single-agent system built on the Anthropic Claude API. It follows a **tool-augmented reasoning** pattern: structured data is gathered by deterministic Python tools, then passed to Claude as context for a final decision.

## Agent Flow

```
agent.py:main()
    │
    ├── for each ticker in WATCHLIST:
    │       │
    │       ├── [Tool] tools/price.py → get_price_data()
    │       │         PSX DPS company page → spot current_price (bid/ask mid)
    │       │         yfinance (.KA) → 3-month OHLCV for trend %, RSI-14, volume
    │       │
    │       ├── [Tool] tools/news.py → get_news_headlines()
    │       │         Scrapes Dawn Business search results
    │       │         Returns: up to 8 headline strings
    │       │
    │       ├── [Tool] tools/psx_announcements.py → get_announcements()
    │       │         Scrapes dps.psx.com.pk/company/<TICKER>
    │       │         Returns: official filings (results, dividends, board meetings)
    │       │         For the top 3 filings, calls tools/pdf_extract.py to download
    │       │         and parse the PDF body to markdown via opendataloader-pdf.
    │       │
    │       ├── [Tool] tools/sentiment.py → analyse_sentiment()
    │       │         Calls Claude (claude-sonnet-4-20250514)
    │       │         Input: company name + headline list
    │       │         Output: {sentiment, score, summary, key_themes}
    │       │
    │       └── [Agent] agent.py → analyse_stock()
    │                 Calls Claude (claude-sonnet-4-20250514)
    │                 Input: price signals + sentiment signals (structured prompt)
    │                 Output: {decision, confidence, reasoning, key_risks, time_horizon}
    │
    └── logger.py → log_decision()
              Persists decision + all signals to db.sqlite
```

## LLM Calls

The agent makes **two-to-three LLM calls per ticker** (model auto-selected
from `LLM_PROVIDER`: OpenAI `gpt-4o-mini` or Anthropic `claude-sonnet-4`):

| Call | Location | Purpose | Max Tokens | When |
|------|----------|---------|------------|------|
| Sentiment scoring | `tools/sentiment.py` | Score news headlines → sentiment JSON | 300 | Always |
| Vision OCR (per scanned filing) | `tools/llm.py:vision_extract` | Rasterised PDF pages → structured financial summary | 1500 | Only on first encounter with a scanned filing |
| Decision making | `agent.py` | Synthesise all signals → ENTER/ADD/HOLD/TRIM/EXIT JSON | 700 | Always |

Sentiment + decision calls enforce **JSON-only output** via system/user
prompt instructions. The decision call uses a `system=` prompt to set the
analyst persona and decision rules. All calls are wrapped in a 3-attempt
rate-limit retry with backoff.

## Decision Rules (encoded in system prompt)

- RSI < 35 → oversold → lean BUY  
- RSI > 70 → overbought → lean SELL  
- Negative sentiment + downtrend → strong SELL  
- Positive sentiment + oversold RSI → potential BUY  
- Official PSX filings are the strongest signal (causal events, not commentary):
  - Filing **PDF body text** is shown to the LLM when extractable (text-based PDFs).
    Model is instructed to cite concrete numbers (Rs/share dividend, dates, agenda).
  - Scanned PDFs (mostly Financial Results and newspaper publications) show
    `(content unavailable: ...)` — model falls back to the title only.
  - Dividend / book closure → bullish bias (esp. for income-style users)
  - Board meeting scheduled → upcoming catalyst, raise confidence
  - Negative regulatory notice / suspension → lean SELL
  - No filings in 30d is normal, do not penalise
- Conflicting signals → default HOLD  
- Confidence 0.5 = conflicting signals, 0.9 = all signals aligned  

## Tool Contracts

### `get_price_data(ticker: str) → dict`
```json
{
  "ticker": "MEBL",
  "current_price": 484.97,
  "trend_30d_pct": -3.2,
  "rsi_14": 38.1,
  "rsi_signal": "neutral",
  "volume_signal": "high",
  "price_source": "psx_dps",
  "yahoo_bar_close": 483.5,
  "psx_bid": 484.0,
  "psx_ask": 485.94,
  "psx_ldcp": 486.0,
  "psx_open": 482.0,
  "psx_high": 487.0,
  "psx_low": 481.0,
  "psx_volume": 1234567
}
```
`price_source` is `psx_dps` when the DPS page yields a spot price, else `yahoo_daily_bar` for `current_price`. Optional **`profile.yaml`** overrides (`broker_last_price_pkr`, `price_overrides`) replace `current_price` after this tool runs (see `tools/profile.py`). Returns `{"error": "...", "ticker": "..."}` on failure — agent handles gracefully.

### `get_news_headlines(company_name: str, max_results: int) → dict`
```json
{
  "company": "Meezan Bank",
  "headlines": ["Headline one", "Headline two"],
  "count": 2,
  "source": "Dawn Business"
}
```

### `get_announcements(ticker, days=30, max_results=10, extract_pdfs=3, max_pdf_chars=2000) → dict`
```json
{
  "ticker": "MEBL",
  "announcements": [
    {
      "date": "Apr 27, 2026",
      "category": "Others",
      "title": "NOTICES OF BOOK CLOSURE FOR THE ENTITLEMENT OF 75% INTERIM CASH DIVIDEND",
      "pdf_url": "https://dps.psx.com.pk/download/document/275225.pdf",
      "content": "...Interim Cash Dividend of Rs. 7.50 per share i.e. 75%...",
      "content_chars": 3262
    },
    {
      "date": "Apr 23, 2026",
      "category": "Financial Results",
      "title": "FINANCIAL RESULTS FOR THE QUARTER ENDED MARCH 31, 2026",
      "pdf_url": "https://dps.psx.com.pk/download/document/275000.pdf",
      "content_error": "no extractable text (likely scanned PDF)"
    }
  ],
  "count": 2,
  "source": "PSX DPS"
}
```
Categories returned: `Financial Results`, `Board Meetings`, `Others`. The top
`extract_pdfs` filings get their full PDF body parsed (text-based PDFs only —
scanned filings need hybrid mode + OCR which is not enabled by default).
Returns `{"error": "...", "announcements": []}` on failure.

### `extract_many(specs, max_chars=2000, use_vision_fallback=True) → dict[doc_id, info]`
Two-stage extractor in `tools/pdf_extract.py`:

1. **Stage 1 — text layer**: batch-runs opendataloader-pdf (one JVM per
   call) to read the PDF's text layer. Auto-discovers a project-local JDK
   at `.jdk/jdk-*` if no system Java is on PATH.
2. **Stage 2 — vision OCR (only if stage 1 found nothing)**: PyMuPDF
   rasterises the PDF to PNGs (110 DPI, max 8 pages), then sends them to
   the provider-aware `vision_extract()` in `tools/llm.py`. Returns a
   structured markdown summary of the filing.

Each result includes a `method` field (`"text-layer"` | `"vision"` |
`"failed"`) so the agent can label the data source in the prompt. Both
raw PDFs (`.cache/pdfs/`) and extracted markdown (`.cache/extracted/`)
are cached by document ID — re-runs are free.

### `vision_extract(image_bytes_list, instruction, max_tokens=1500) → str` (in `tools/llm.py`)
Provider-aware multi-image vision call. Auto-routes to OpenAI's
`gpt-4o-mini` or Anthropic's `claude-sonnet-4` depending on
`LLM_PROVIDER` (same env-var resolution as the decision call). Wrapped in
a 3-attempt rate-limit retry with exponential backoff so a single
scanned-PDF burst doesn't crash the run.

### `analyse_sentiment(company_name: str, headlines: list) → dict`
```json
{
  "company": "Meezan Bank",
  "sentiment": "positive",
  "score": 0.6,
  "summary": "Bank reports strong quarterly earnings amid rising deposits.",
  "key_themes": ["earnings", "deposits"]
}
```
Returns neutral defaults when no headlines are provided.

### `log_decision(...) → decision_id: str`
Writes to `decisions` table. Returns UUID used to link future outcomes.

## Evaluation Loop

`eval.py` closes the feedback loop:

```
eval.py --record
    ├── get_open_decisions()      ← decisions with no outcome yet
    ├── fetch_current_price()     ← get_price_data() (PSX DPS spot, Yahoo fallback)
    └── record_outcome()          ← writes pnl_pct to outcomes table

eval.py (no args)
    └── compute_metrics()         ← win rate, avg P&L per ticker
```

P&L is computed as: `((price_after - price_at_decision) / price_at_decision) * 100`  
A BUY decision is a "win" if `pnl_pct > 0`; a SELL decision is a "win" if `pnl_pct < 0`.

## Extending the Agent

| Goal | Where to change |
|------|----------------|
| Add a new ticker | `WATCHLIST` dict in `agent.py` |
| Change news source | `tools/news.py` → `get_news_headlines()` |
| Add a new signal (e.g. P/E ratio) | New file in `tools/`, import in `agent.py`, append to `user_prompt` |
| Add memory / multi-turn reasoning | Replace single `client.messages.create()` call with a loop or use the Anthropic Agents SDK |
| Switch to tool_use API | Replace string-formatted prompt with `tools=` parameter in `client.messages.create()` |
