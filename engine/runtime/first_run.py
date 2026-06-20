# FILE: engine/runtime/first_run.py
# REPLACE ENTIRE FILE WITH THIS EXACT CONTENT

from __future__ import annotations

import sqlite3
import time

from engine.runtime.config_schema import load_runtime_config
from engine.runtime.db_guard import ensure_db_ok
from engine.runtime.jobs.repair_schema import run as repair_schema
from engine.runtime.lifecycle_state import set_state, SCHEMA_REPAIR, WARMING_UP


def _db_path() -> str:
    cfg = load_runtime_config()
    return str(getattr(cfg, "DB_PATH", "") or "").strip()


def _seed_minimum_rows(db_path: str) -> dict:
    """
    Non-technical deterministic boot:
    - Ensure some core rows exist so UI has something to read
    """
    out = {"ok": True, "seeded": [], "error": None}
    now_ms = int(time.time() * 1000)

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")

        cur = conn.cursor()

        # portfolio_state (schema in repair_schema.py uses ts_ms PRIMARY KEY)
        try:
            n = cur.execute("SELECT COUNT(*) FROM portfolio_state").fetchone()
            if int(n[0] or 0) == 0:
                cur.execute(
                    "INSERT INTO portfolio_state (ts_ms, equity, drawdown) VALUES (?, ?, ?)",
                    (now_ms, 0.0, 0.0),
                )
                out["seeded"].append("portfolio_state")
        except Exception:
            pass

        # broker_account (schema in repair_schema.py uses ts_ms PRIMARY KEY)
        try:
            n = cur.execute("SELECT COUNT(*) FROM broker_account").fetchone()
            if int(n[0] or 0) == 0:
                cur.execute(
                    "INSERT INTO broker_account (ts_ms, equity, buying_power) VALUES (?, ?, ?)",
                    (now_ms, 0.0, 0.0),
                )
                out["seeded"].append("broker_account")
        except Exception:
            pass

        conn.commit()
        conn.close()
        return out

    except Exception as e:
        out["ok"] = False
        out["error"] = str(e)
        return out


def bootstrap_first_run(mode: str = "safe") -> dict:
    """
    Deterministic boot pipeline:
    1) DB guard (creates file, quarantines corruption)
    2) Schema repair (idempotent)
    3) Seed minimal rows (idempotent)
    """
    out = {"ok": True, "mode": mode, "db_guard": None, "schema": None, "seed": None}

    try:
        set_state(SCHEMA_REPAIR, "first_run_bootstrap")
    except Exception:
        pass

    g = ensure_db_ok()
    out["db_guard"] = g
    if not g.get("ok"):
        out["ok"] = False
        return out

    s = repair_schema()
    out["schema"] = s
    if not s.get("ok"):
        out["ok"] = False

    db_path = _db_path()
    if db_path:
        out["seed"] = _seed_minimum_rows(db_path)
    else:
        out["seed"] = {"ok": False, "error": "DB_PATH_missing_after_schema"}

    try:
        set_state(WARMING_UP, "awaiting_first_price_tick")
    except Exception:
        pass

    return out