import sqlite3
import json
import uuid
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "db.sqlite"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                decision TEXT NOT NULL,
                confidence REAL,
                signals TEXT,
                reasoning TEXT,
                price_at_decision REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS outcomes (
                decision_id TEXT PRIMARY KEY,
                price_after_nd REAL,
                n_days INTEGER,
                pnl_pct REAL,
                recorded_at TEXT,
                FOREIGN KEY (decision_id) REFERENCES decisions(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price_pkr REAL,
                decision_id TEXT,
                executed_at TEXT NOT NULL,
                notes TEXT,
                FOREIGN KEY (decision_id) REFERENCES decisions(id)
            )
        """)
        conn.commit()

def log_decision(ticker, decision, confidence, signals, reasoning, price_at_decision):
    decision_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO decisions
                (id, ticker, timestamp, decision, confidence, signals, reasoning, price_at_decision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (decision_id, ticker, datetime.utcnow().isoformat(), decision.upper(),
              confidence, json.dumps(signals), reasoning, price_at_decision))
        conn.commit()
    print(f"  [log] Saved decision {decision_id[:8]}... for {ticker}")
    return decision_id

def record_outcome(decision_id, price_after, n_days):
    with get_conn() as conn:
        row = conn.execute("SELECT price_at_decision FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if not row:
            print(f"  [log] Decision {decision_id} not found.")
            return
        price_at = row["price_at_decision"]
        pnl_pct = ((price_after - price_at) / price_at) * 100 if price_at else None
        conn.execute("""
            INSERT OR REPLACE INTO outcomes
                (decision_id, price_after_nd, n_days, pnl_pct, recorded_at)
            VALUES (?, ?, ?, ?, ?)
        """, (decision_id, price_after, n_days, round(pnl_pct, 4) if pnl_pct else None, datetime.utcnow().isoformat()))
        conn.commit()
    print(f"  [log] Outcome recorded: {pnl_pct:.2f}%")

def log_execution(
    ticker,
    action,
    quantity,
    price_pkr=None,
    decision_id=None,
    notes=None,
):
    """Append a broker-executed trade (trim, buy, exit, …) for audit / eval context.

    ``quantity`` is always positive (shares trimmed, entered, exited, etc.).
    ``decision_id`` may be omitted; if set, should match a row in ``decisions``.
    """
    exec_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO executions
                (id, ticker, action, quantity, price_pkr, decision_id, executed_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exec_id,
                ticker.upper(),
                (action or "").upper(),
                int(quantity),
                float(price_pkr) if price_pkr is not None else None,
                decision_id,
                datetime.utcnow().isoformat(),
                notes,
            ),
        )
        conn.commit()
    print(f"  [log] Saved execution {exec_id[:8]}... {ticker.upper()} {action.upper()} x{quantity}")
    return exec_id

DATA_DIR = Path(__file__).parent / "data"
DECISIONS_JSON = DATA_DIR / "decisions.json"


def export_decisions_json():
    """Export all decisions + outcomes to data/decisions.json for git persistence.

    Safe to call after every run — appends/overwrites the full history so the
    file is always a complete snapshot. Omits raw signals blob to keep the file
    readable; reasoning is included.
    """
    DATA_DIR.mkdir(exist_ok=True)
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                d.id, d.ticker, d.timestamp, d.decision, d.confidence,
                d.price_at_decision, d.reasoning,
                o.price_after_nd, o.n_days, o.pnl_pct, o.recorded_at
            FROM decisions d
            LEFT JOIN outcomes o ON d.id = o.decision_id
            ORDER BY d.timestamp DESC
        """).fetchall()
    records = [dict(r) for r in rows]
    DECISIONS_JSON.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"  [export] {len(records)} decisions → {DECISIONS_JSON}")
    return str(DECISIONS_JSON)


def get_open_decisions():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT d.* FROM decisions d
            LEFT JOIN outcomes o ON d.id = o.decision_id
            WHERE o.decision_id IS NULL
            ORDER BY d.timestamp DESC
        """).fetchall()
    return [dict(r) for r in rows]
