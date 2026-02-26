import sqlite3
from engine.runtime.config_schema import load_runtime_config


def run():
    try:
        cfg = load_runtime_config()
        db_path = cfg.DB_PATH
    except Exception as e:
        return {"ok": False, "error": f"config_load_failed: {e}"}

    if not db_path:
        return {"ok": False, "error": "DB_PATH not set"}

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # ---- REQUIRED TABLES ----

    cur.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_ms INTEGER,
        severity TEXT,
        symbol TEXT,
        horizon_s INTEGER,
        message TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS portfolio_state (
        ts_ms INTEGER PRIMARY KEY,
        equity REAL,
        drawdown REAL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS broker_account (
        ts_ms INTEGER PRIMARY KEY,
        equity REAL,
        buying_power REAL
    )
    """)

    # ---- FIX labels columns ----

    cur.execute("PRAGMA table_info(labels)")
    cols = [r[1] for r in cur.fetchall()]

    if "label" not in cols:
        cur.execute("ALTER TABLE labels ADD COLUMN label TEXT")

    if "ts_ms" not in cols:
        cur.execute("ALTER TABLE labels ADD COLUMN ts_ms INTEGER")

    conn.commit()
    conn.close()

    return {"ok": True}
