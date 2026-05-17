# Remote runbook — commands, paths & Cowork prompts

Use this from **SSH**, **Claude Cowork**, or any machine where the repo is cloned. Replace `PROJECT_ROOT` with your clone path (example: `/Users/you/psx-agent`).

Set once per shell:

```bash
export PSX_AGENT_ROOT="/path/to/psx-agent"   # e.g. ~/Documents/personal_projects/psx-agent
cd "$PSX_AGENT_ROOT"
```

---

## One-line paths (for copy-paste into Cowork / notes)

| Artifact | Path (relative to repo root) | Stable “always read this” file |
|----------|--------------------------------|--------------------------------|
| Scout pre-flight markdown | `reports/SCOUT_LATEST.md` | **yes** |
| Scout archive (timestamped) | `reports/scout-YYYYMMDD-HHMMSS.md` | no |
| Full analysis report markdown | `reports/LATEST.md` | **yes** |
| Analysis archive | `reports/psx-report-YYYYMMDD-HHMMSS.md` | no |
| Verdict / continuity cache (used when regenerating report) | `.cache/report_verdict_inject.md` | internal |
| Decisions DB | `db.sqlite` | — |
| Your portfolio & cash | `profile.yaml` | — |
| Env / API keys | `.env` | — |

**Absolute examples** (swap in your `PSX_AGENT_ROOT`):

```
$PSX_AGENT_ROOT/reports/SCOUT_LATEST.md
$PSX_AGENT_ROOT/reports/LATEST.md
$PSX_AGENT_ROOT/db.sqlite
$PSX_AGENT_ROOT/profile.yaml
```

---

## Prerequisites (remote machine)

```bash
cd "$PSX_AGENT_ROOT"
python3 -m pip install -r requirements.txt
# Optional heavy crawler stack:
# python3 -m pip install -r requirements-crawl4ai.txt
cp .env.example .env   # if needed
# Edit .env: ANTHROPIC_API_KEY and/or OPENAI_API_KEY, LLM_PROVIDER, etc.
```

---

## Tools as commands

Run from `$PSX_AGENT_ROOT`.

### 1. Scout (cheap gate — run daily)

```bash
python3 scout.py
```

- Writes: `reports/SCOUT_LATEST.md` + `reports/scout-*.md`
- Logs: `scout_runs` table in `db.sqlite`

```bash
python3 scout.py --verbose              # more headline detail in terminal
python3 scout.py --json                 # machine-readable + report paths in JSON
python3 scout.py --no-llm               # heuristic only, no LLM cost
python3 scout.py --no-report            # stdout/DB only, no markdown files
python3 scout.py --auto-urgent-only     # if URGENT, subprocess: python3 agent.py
python3 scout.py --auto                 # if RUN or URGENT, runs agent.py
```

### 2. Full analysis agent (heavy — when Scout says RUN/URGENT or on your schedule)

```bash
python3 agent.py
```

- Appends decisions to `db.sqlite`
- Refreshes analysis reports via agent’s own hook (see project behavior); you can always regenerate below.

### 3. Regenerate analysis markdown from DB (no new LLM calls for tickers)

```bash
python3 report.py
```

- Writes: `reports/LATEST.md` + `reports/psx-report-*.md`
- Merges verdict block from `.cache/report_verdict_inject.md` when present

```bash
python3 report.py --full              # all DB history in report
python3 report.py -o /tmp/out.md      # custom output only
```

### 4. Evaluation (outcomes & metrics)

```bash
python3 eval.py                       # aggregate metrics
python3 eval.py --record              # interactive outcome recording
```

### 5. Sync broker cash in profile (optional)

```bash
python3 sync_broker.py
python3 sync_broker.py --broker-cash 3589 --external-cash 30000 --write
```

### 6. Quick sanity checks

```bash
python3 -c "from tools.kse100 import fetch_kse100_snapshot; print(fetch_kse100_snapshot())"
python3 -c "from tools.price import get_price_data; print(get_price_data('MEBL'))"
```

---

## Suggested daily workflow (remote)

```bash
cd "$PSX_AGENT_ROOT"
python3 scout.py
# Read: reports/SCOUT_LATEST.md
# If SKIP → stop. If RUN or URGENT → continue:

python3 agent.py
python3 report.py
# Read: reports/LATEST.md
```

---

## Prompts for Claude Cowork (after each artifact)

Paste the **path** into Cowork so it reads the file, then use the matching prompt.

### A. After **Scout** — `reports/SCOUT_LATEST.md`

```
Read the file at PROJECT_ROOT/reports/SCOUT_LATEST.md (full path: paste absolute path).

Summarize in 5 bullets: verdict, KSE100 snapshot, whether anything URGENT, hours since last full run, and whether I should run the full agent today.

End with one sentence: "Run full analysis: yes/no — because …"
```

### B. After **full analysis** — `reports/LATEST.md`

```
Read PROJECT_ROOT/reports/LATEST.md.

Give me:
1) Portfolio verdict / primary action if present
2) Per-holding actions that affect my real positions (ENTER/ADD/TRIM/EXIT/HOLD) with ticker and rupee size if stated
3) Anything that conflicts with profile.yaml caps or cash — flag it
4) Top 3 risks called out in the report

End with: what I should actually do this week (max 1 buy + 1 trim), in plain language — not financial advice.
```

### C. **Combined** — Scout + Analysis same session

```
I ran scout and the full agent. Read these two files in order:
1) PROJECT_ROOT/reports/SCOUT_LATEST.md
2) PROJECT_ROOT/reports/LATEST.md

Produce:
- Executive summary (8 sentences max)
- Contradictions between scout context and analysis (if any)
- Single prioritized action list for the next 7 days
- Reminder to verify prices against my broker before trading

Assume I want low churn (monthly trades, not day trading).
```

### D. After **`eval.py`** (metrics)

```
I ran: cd PROJECT_ROOT && python3 eval.py
Paste the terminal output below (or read outcomes from db if you have SQL access).

Interpret win-rate and average P&L cautiously; note small sample bias. Suggest whether to tighten profile risk settings — config suggestions only, no code edits.
```

---

## Remote SSH example

```bash
ssh user@host
cd /path/to/psx-agent
source .venv/bin/activate   # if you use a venv
python3 scout.py && python3 agent.py && python3 report.py
```

Then **pull or open** `reports/SCOUT_LATEST.md` and `reports/LATEST.md` via SFTP, VS Code Remote, or `cat` in terminal.

---

## Env knobs (optional)

| Variable | Purpose |
|----------|---------|
| `LLM_PROVIDER` | `anthropic` / `openai` |
| `LLM_MODEL` | Override model id |
| `SCOUT_LOOKBACK_HOURS` | Headline age window (default 72) |
| `SCOUT_SHOCK_WINDOW_HOURS` | Shock-keyword window (default 48) |
| `SCOUT_FORCE_RUN_AFTER_HOURS` | Lean RUN after N hours since last agent run (default 168) |
| `PSX_SHOCK_MODE` | Set `1` when running `agent.py` in crisis mode |

---

## Disclaimer

Outputs are research aids. Confirm prices, fees, and tax with your broker and adviser before acting.
