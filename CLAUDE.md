# psx-agent

A Claude-powered stock analysis agent for the Pakistan Stock Exchange (PSX).

## Project Overview

This agent fetches real-time price data and news for a watchlist of PSX-listed companies, scores sentiment using Claude, and outputs a structured BUY / HOLD / SELL decision with confidence ratings. All decisions are persisted to a local SQLite database so outcomes can be tracked and evaluated over time.

## Project Structure

```
psx-agent/
├── agent.py           # Main entry point — runs the full analysis pipeline
├── eval.py            # Evaluation CLI — record outcomes and compute metrics
├── sync_broker.py     # Reconcile profile.yaml's investable_cash_pkr with broker
├── logger.py          # SQLite helpers (init_db, log_decision, record_outcome)
├── profile.yaml       # User profile: capital, risk, holdings, preferences
├── requirements.txt   # Python dependencies
├── .env.example       # API key template
├── db.sqlite          # Created at runtime — stores decisions and outcomes
└── tools/
    ├── __init__.py
    ├── price.py             # PSX DPS spot quote + yfinance OHLC for RSI/trend/volume
    ├── psx_quote.py         # Scrape dps.psx.com.pk/company/<TICKER> (stats grid)
    ├── news.py              # Dawn Business scraper — headline fetcher
    ├── psx_announcements.py # PSX DPS scraper — official corporate filings per ticker
    ├── pdf_extract.py       # Downloads + parses filing PDFs via opendataloader-pdf
    └── sentiment.py         # Claude API call — sentiment scoring from headlines
```

## Setup

```bash
# 1. Install dependencies
pip3 install -r requirements.txt

# 2. Configure API key
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-...

# 3. Java 11+ required for PDF filing extraction (opendataloader-pdf)
#    Either install system-wide (brew install openjdk@17) OR drop a
#    Temurin tarball into .jdk/ — the agent auto-discovers .jdk/jdk-* and
#    sets JAVA_HOME for you.
```

### PDF filing cache + vision OCR fallback

`tools/pdf_extract.py` runs a **two-stage extraction pipeline**:

1. **Text-layer extraction** (free, fast, deterministic). Uses
   opendataloader-pdf via the project-local JDK. Works for digital PDFs
   (board meeting notices, dividend declarations, regulatory letters).
2. **Vision-OCR fallback** (paid, only when stage 1 returns no text).
   PyMuPDF rasterises the PDF to PNG images, then sends them to whichever
   vision model `LLM_PROVIDER` resolves to (OpenAI `gpt-4o-mini` or
   Anthropic `claude-sonnet-4`). The model returns a structured markdown
   summary of the financial filing (revenue, profit, EPS, dividend per
   share, YoY changes). This unlocks scanned quarterly/annual results.

Both stages cache to `.cache/extracted/<doc_id>.md`. Vision-derived
summaries carry an HTML-comment header (`<!-- extracted-via: vision -->`)
so re-runs know not to re-pay for them. PSX document IDs are immutable —
cache never goes stale.

## Running the Agent

```bash
# Run a full analysis across all watchlist tickers
python3 agent.py
```

Output: colour-coded BUY / HOLD / SELL report printed to stdout; each decision persisted to `db.sqlite`.

## Evaluating Past Decisions

```bash
# Interactively record an outcome price against an open decision
python3 eval.py --record

# Print win-rate and P&L metrics across all scored decisions
python3 eval.py
```

## Syncing Cash with Broker

The agent treats `capital.investable_cash_pkr` in `profile.yaml` as your
total dry powder for the strategy: cash actually sitting in the broker's
ledger PLUS any external cash (bank, savings) you'd realistically deploy
on a BUY signal. If this drifts from reality, position-sizing and the
cash-reserve guardrail get wrong.

```bash
# Interactive — prompts for broker ledger balance + external cash,
# shows reconciliation against live position values, asks before writing.
python3 sync_broker.py

# Non-interactive — useful from a wrapper script:
python3 sync_broker.py --broker-cash 3589 --external-cash 30000 --write
```

The script regex-edits only the `investable_cash_pkr` line in
`profile.yaml`, preserving every comment and other field exactly. Run it
whenever you deposit/withdraw cash, after each broker statement, or just
to sanity-check the agent's view against your broker app.

## Watchlist

Defined in `agent.py` → `WATCHLIST` dict. Default tickers:

| Ticker | Company |
|--------|---------|
| MEBL   | Meezan Bank |
| TGL    | Tariq Glass |
| OGDC   | Oil and Gas Development Company |
| BOP    | Bank of Punjab |
| WTL    | WorldCall Telecom |

To add or remove tickers edit the `WATCHLIST` dict. **Spot `current_price`** comes from **PSX DPS** (`dps.psx.com.pk/company/<TICKER>`); Yahoo (`.KA`) still backs **30d trend, RSI-14, volume** in `tools/price.py`. Optional **`broker_last_price_pkr` / `price_overrides`** in `profile.yaml` override the DPS figure when you want an exact broker LTP.

## Key Environment Variables

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Required if `LLM_PROVIDER=anthropic` (or only Anthropic key set). |
| `OPENAI_API_KEY` | Required if `LLM_PROVIDER=openai` (or only OpenAI key set). |
| `LLM_PROVIDER` | `openai` or `anthropic`. Defaults: openai if only OpenAI key set, else anthropic. |
| `LLM_MODEL` | Override decision-making model (default `gpt-4o-mini` / `claude-sonnet-4-20250514`). |
| `LLM_VISION_MODEL` | Override vision-OCR model (defaults same as `LLM_MODEL`). |

## Model

Both `agent.py` and `tools/sentiment.py` use `claude-sonnet-4-20250514`. Update the `model=` argument in each file if you want to switch models.

## Database Schema

**decisions** — one row per agent run per ticker  
**outcomes** — joined by `decision_id`; populated via `eval.py --record`

Run `python3 eval.py` to see aggregated win-rate and average P&L once at least one outcome has been recorded.

## Linting / Tests

No test suite yet. To verify the tools in isolation:

```bash
python3 -c "from tools.price import get_price_data; print(get_price_data('MEBL'))"
python3 -c "from tools.news import get_news_headlines; print(get_news_headlines('Meezan Bank'))"
python3 -m tools.psx_announcements MEBL
python3 -m tools.pdf_extract https://dps.psx.com.pk/download/document/275225.pdf
```
