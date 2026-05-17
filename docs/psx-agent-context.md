# PSX Agent — context from product / UX discussion

This note captures decisions and explanations from working on **reports**, **sizing**, **disclaimers**, and **profile risk settings**. It is meant as a single place to re-read *why* things behave the way they do.

---

## 1. Latest report: one run only (not full DB history)

**Goal:** `reports/LATEST.md` (and the timestamped copy after each run) should reflect **only the latest `agent.py` invocation**, not every row ever stored in `db.sqlite`.

**Implementation (`report.py`):**

- Default **`build_markdown(latest_batch_only=True)`** scopes rows to the **last agent run**.
- Runs are detected with **`_agent_runs()`**: decisions are processed in time order; when a **ticker repeats**, a new run starts (each run visits each symbol at most once). This avoids merging **two full passes** that happen within a few minutes — an issue the older **15-minute time gap** clustering caused.
- **`python3 report.py`** → latest run only.
- **`python3 report.py --full`** → full history in one markdown file.

---

## 1b. Spot price: PSX DPS, not Yahoo

**`current_price`** in the pipeline comes from **`https://dps.psx.com.pk/company/<TICKER>`** (bid/ask mid when both exist). Yahoo (`.KA`) is only for **historical bars** → 30d trend, RSI-14, volume. If you need an exact broker last trade, set **`broker_last_price_pkr`** (or **`price_overrides`**) in **`profile.yaml`** — that wins after the DPS fetch.

---

## 2. Executable BUY sizing (cash + caps, not the LLM)

**Goal:** Rupee and share counts for **ENTER/ADD** should follow **your profile and portfolio math**, not free-form model guesses.

**Implementation:**

- **`tools/portfolio.py` → `compute_buy_allocation()`** computes:
  - **`deployable_cash_pkr`** = cash minus the **minimum cash floor** (see §5).
  - **`max_spend`** = minimum of:
    - deployable cash,
    - room under **`max_single_position_pct`** (for **ADD**, subtract current position MV),
    - room under **`max_sector_exposure_pct`** vs current sector market value.
  - Whole shares: **`floor(max_spend / price)`**.
- **`agent.py` → `finalize_buy_sizes()`** applies sizing **only to the portfolio verdict’s primary** ENTER/ADD. Other ENTER/ADD rows stay **signal-only** (0 shares) with a short note.
- The stock-level LLM is instructed to output **`shares: 0`** and **`size_pkr: 0`** for ENTER/ADD; **TRIM/EXIT** sizing is still model-suggested.
- Decisions are **logged after** verdict + sizing so `db.sqlite` matches the report.

---

## 3. “Research vs order ticket”

Per-ticker output is **planning**: the **primary** line in the **portfolio verdict** is the one sized to your cash rules. The model does not place broker orders; you still confirm size, price, and fees.

---

## 4. Report layout: holdings vs recommendations

**Goal:** Separate **what you own** from **this run’s analysis**.

**Implementation (`report.py`):**

- **`## Current holdings`** — table from **`profile.yaml` holdings** + live prices (renamed from “Portfolio snapshot”).
- **`## This run — recommendations & analysis`** with:
  - **`### Names you already hold`** — decisions for tickers in profile holdings.
  - **`### Watchlist & other names (new cash / ideas)`** — everything else.
- Per-ticker detail uses **`####`** headings under those subsections.

**Extra copy when `system_sizing` is present:**

- **`Why this share count?`** — min of cash-after-reserve, single-name %, sector %.
- **`Not the full Rs … cash`** — when **single-name cap** is tighter than deployable cash (e.g. only **1 share** of an expensive name because **20% of equity** caps the new line).

---

## 5. Deployable cash and `min_cash_reserve_pct`

**Deployable cash** (for sizing / report):

\[
\text{deployable} = \max(0,\ \text{cash} - \text{min\_cash\_floor})
\]

\[
\text{min\_cash\_floor} = \frac{\text{min\_cash\_reserve\_pct}}{100} \times \text{total\_equity}
\]

- **`cash`** = **`investable_cash_pkr`** fed through **`build_portfolio()`** as **`cash_pkr`**.
- **Total equity** = market value of holdings **+** that cash.

**Example:** Cash Rs 25,000, total equity Rs 65,920, **`min_cash_reserve_pct: 10`**  
→ floor = Rs 6,592 → deployable = **Rs 18,408**.

**`min_cash_reserve_pct`** is **your** risk preference in **`profile.yaml`** (example default **10**). It is **not** computed from the market. It means: “treat at least X% of portfolio equity as cash that should not be spent on new buys.” Adjust it to match how much dry powder you actually want.

---

## 6. Why only one share sometimes (e.g. NESTLE)

Even with **high deployable cash**, **`max_single_position_pct`** (e.g. **20%**) caps how large a **new** position can be as a fraction of **total equity**.

Example: max new position ≈ **Rs 13,184**, price ≈ **Rs 7,489/share** → **1 share** fits; **2 shares** would exceed the cap.

To allow a larger line (if you accept concentration risk), raise **`max_single_position_pct`** in **`profile.yaml`**.

---

## 7. Verdict disclaimer: no fake “October 2023” cutoff

The portfolio verdict LLM sometimes invented disclaimers like “data up to October 2023.” That was **not** from your data.

**Fix (`agent.py`):**

- **`verdict_disclaimer()`** is always used for the written verdict (model **`disclaimer` JSON is not trusted**).
- Verdict system prompt forbids claiming training cutoffs.
- Stock-level system prompt: do not claim “data up to [year]” in reasoning.

---

## 8. Primary continuity (last run vs this run)

After each run, the agent reads the **sized primary** from the **previous** completed batch in `db.sqlite` and compares it to **this run’s** primary.

- **Report:** A **## Primary continuity (last run vs this run)** block is inserted **above** the portfolio verdict. It includes a short **Quick take**, a **lean** (`prior` / `current` / `tie` / etc.), and a narrative grounded in **today’s** snapshots for **both** tickers (prices, RSI, reasoning excerpts).
- **Implementation:** `report.get_last_completed_primary()` + `agent.synthesize_primary_continuity()` (LLM). If the API call fails, a **rule-based fallback** compares confidences.
- **Disable LLM for this step:** set env `PRIMARY_CONTINUITY_LLM=0` to use only the heuristic block (saves tokens).

### Why continuity / verdict disappeared from `LATEST.md`

The verdict block is inserted **before** `## Summary` only when `write_reports(..., verdict_section=…)` receives the markdown from `agent.py`. Running **`python3 report.py` alone** rebuilds from the DB and used to pass **no** verdict — so **portfolio verdict + continuity vanished**.

**Fix:** `agent.py` now calls **`persist_verdict_inject()`**, saving the combined block to **`.cache/report_verdict_inject.md`**. **`report.py`** merges that cache whenever it regenerates reports (unless **`--no-inject`**). Run **`python3 agent.py` once** to refresh the cache; afterwards **`python3 report.py`** keeps verdict + continuity in `LATEST.md`.

---

## 9. Quick command reference

| Command | Effect |
|--------|--------|
| `python3 agent.py` | Full run; refreshes DB + reports (latest run scoped in `LATEST.md`). |
| `python3 report.py` | Regenerate report from DB (latest run only). |
| `python3 report.py --full` | Full decision history in markdown. |

---

## 10. Files touched in this workstream

| File | Role |
|------|------|
| `report.py` | Latest-run scope, holdings vs watchlist sections, sizing explanations. |
| `agent.py` | Deferred logging, `finalize_buy_sizes`, verdict disclaimer, LLM sizing instructions. |
| `tools/portfolio.py` | `compute_buy_allocation()`. |

---

_End of context note. Update this file when behaviour or defaults change._
