import sqlite3
import time
from engine.runtime.config_schema import load_runtime_config

SCHEMA_VERSION = 1
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

    # ---- RUNTIME META (single source of truth) ----
    cur.execute("""
    CREATE TABLE IF NOT EXISTS runtime_meta (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_ts_ms INTEGER
    )
    """)

    # Record schema version (idempotent)
    try:
        now = int(time.time() * 1000)
        cur.execute(
            """
            INSERT INTO runtime_meta(key, value, updated_ts_ms)
            VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts_ms=excluded.updated_ts_ms
            """,
            ("schema_version", str(int(SCHEMA_VERSION)), int(now)),
        )
    except Exception:
        pass

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
        equity REAL NOT NULL DEFAULT 0,
        drawdown REAL NOT NULL DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS broker_account (
        ts_ms INTEGER PRIMARY KEY,
        equity REAL NOT NULL DEFAULT 0,
        buying_power REAL NOT NULL DEFAULT 0
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
