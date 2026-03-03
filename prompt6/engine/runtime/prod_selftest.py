# prod_selftest.py
"""
Hard startup self-test (fail-fast).

Runs:
- DB init
- core module imports
- inserts a synthetic event
- runs process_events once to embed + predict
- verifies event_embeddings + predictions rows exist

Usage:
  python prod_selftest.py
"""

import json
import time
import traceback

from engine.storage import connect, init_db
from engine.validation import init_validation_db


def _now_ms() -> int:
    return int(time.time() * 1000)


def _require(cond: bool, msg: str):
    if not cond:
        raise RuntimeError(msg)


def main() -> int:
    # 1) DB schema
    init_db()
    init_validation_db()

    # 2) Import core modules (crash here => broken deployment)
    import engine.strategy.predictor  # noqa: F401
    import engine.strategy.model_v2   # noqa: F401
    import engine.strategy.learning   # noqa: F401
    import engine.runtime.alerts     # noqa: F401
    import engine.execution.kill_switch  # noqa: F401
    import engine.strategy.capital_guard  # noqa: F401

    # 3) Insert synthetic event (unique key)
    ts_ms = _now_ms()
    event_key = f"__selftest__{ts_ms}"
    title = f"SELFTEST EVENT {ts_ms}"
    body = "selftest body"

    con = connect()
    try:
        con.execute("BEGIN IMMEDIATE;")

        con.execute(
            """
            INSERT OR IGNORE INTO events(ts_ms, source, title, body, url, event_key)
            VALUES (?,?,?,?,?,?)
            """,
            (ts_ms, "selftest", title, body, "", event_key),
        )
        row = con.execute("SELECT id FROM events WHERE event_key=?", (event_key,)).fetchone()
        con.execute("COMMIT;")
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        try:
            con.close()
        except Exception:
            pass

    _require(row is not None and row[0] is not None, "selftest: failed to insert event")
    eid = int(row[0])

    # 4) Run event processing once (embedding + predictions write)
    import process_events
    process_events.main()

    # 5) Verify embedding + prediction rows exist
    con = connect()
    try:
        emb = con.execute("SELECT dim, length(vec) FROM event_embeddings WHERE event_id=?", (eid,)).fetchone()
        _require(emb is not None, "selftest: missing event_embeddings row")
        _require(int(emb[0] or 0) > 0, "selftest: embedding dim invalid")

        pred = con.execute("SELECT COUNT(*) FROM predictions WHERE event_id=?", (eid,)).fetchone()
        n_pred = int(pred[0] or 0) if pred else 0
        _require(n_pred > 0, "selftest: missing predictions rows")

        # 6) Verify symbol-aware news embeddings exist (best-effort; fail-fast if table exists but empty)
        row = con.execute(
            "SELECT COUNT(*) FROM news_symbol_embeddings WHERE event_id=?",
            (eid,),
        ).fetchone()
        n_symemb = int(row[0] or 0) if row else 0
        _require(n_symemb > 0, "selftest: missing news_symbol_embeddings rows")

        # 7) Verify provenance columns, if present
        cols = [r[1] for r in con.execute("PRAGMA table_info(news_symbol_embeddings)").fetchall()]
        if "method" in cols:
            row2 = con.execute(
                "SELECT COUNT(*) FROM news_symbol_embeddings WHERE event_id=? AND (method IS NULL OR method='')",
                (eid,),
            ).fetchone()
            n_missing = int(row2[0] or 0) if row2 else 0
            _require(n_missing == 0, "selftest: news_symbol_embeddings.method missing")
        if "created_ts_ms" in cols:
            row3 = con.execute(
                "SELECT COUNT(*) FROM news_symbol_embeddings WHERE event_id=? AND COALESCE(created_ts_ms,0) <= 0",
                (eid,),
            ).fetchone()
            n_missing2 = int(row3[0] or 0) if row3 else 0
            _require(n_missing2 == 0, "selftest: news_symbol_embeddings.created_ts_ms missing")
    finally:
        try:
            con.close()
        except Exception:
            pass

    out = {
        "status": "ok",
        "event_id": eid,
        "ts_ms": ts_ms,
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        err = {
            "status": "error",
            "error": str(e),
            "trace": traceback.format_exc(),
        }
        print(json.dumps(err, indent=2, sort_keys=True))
        raise SystemExit(2)
