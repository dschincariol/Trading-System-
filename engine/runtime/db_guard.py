# NEW FILE: engine/runtime/db_guard.py
# CREATE THIS FILE WITH THIS EXACT CONTENT:

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path

from engine.runtime.config_schema import load_runtime_config


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime())


def _resolve_db_path() -> Path:
    cfg = load_runtime_config()
    p = str(getattr(cfg, "DB_PATH", "") or "").strip()
    if not p:
        p = os.environ.get("DB_PATH", "dev.db")
    return Path(p).expanduser().resolve()


def ensure_db_ok() -> dict:
    """
    Deterministic DB guard:
    - Ensure parent dir exists
    - Open DB (creates file if missing)
    - quick_check; if fails -> quarantine + recreate empty DB
    """
    out = {"ok": True, "db_path": None, "action": "none", "error": None}

    db_path = _resolve_db_path()
    out["db_path"] = str(db_path)

    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        out["ok"] = False
        out["error"] = f"mkdir_failed:{e}"
        return out

    # Create/open
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.commit()
        conn.close()
    except Exception as e:
        out["ok"] = False
        out["error"] = f"open_failed:{e}"
        return out

    # quick_check (fast)
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("PRAGMA quick_check;").fetchone()
        conn.close()

        ok = bool(row and str(row[0]).strip().lower() == "ok")
        if ok:
            return out
    except Exception:
        ok = False

    # Quarantine and recreate
    try:
        tag = _now_tag()
        quarantine = db_path.with_suffix(db_path.suffix + f".corrupt_{tag}")
        try:
            shutil.copy2(str(db_path), str(quarantine))
        except Exception:
            # fallback: move
            try:
                shutil.move(str(db_path), str(quarantine))
            except Exception:
                pass

        # recreate empty db
        try:
            if db_path.exists():
                db_path.unlink()
        except Exception:
            pass

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.commit()
        conn.close()

        out["action"] = f"recreated_from_corruption:{quarantine.name}"
        return out
    except Exception as e:
        out["ok"] = False
        out["error"] = f"recreate_failed:{e}"
        return out