# engine/model_registry.py
"""
Canonical model registry (SQLite).

Used by:
- engine/runtime/guards.py (auto rollback uses get_stage_latest)
- strategy jobs (register/promote/rollback)

Single source of truth. Other locations should import this module.
"""

import json
import os
import time
import logging
from typing import Optional, Dict, Any, List, Tuple, Union

from engine.runtime.storage import connect as _connect
from engine.runtime.storage import init_db as _init_db

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [model_registry] %(message)s",
)

# Canonical schema (regime-based)
SCHEMA = """
CREATE TABLE IF NOT EXISTS model_registry (
  model_name   TEXT NOT NULL,
  model_kind   TEXT NOT NULL,
  model_ts_ms  INTEGER NOT NULL,
  stage        TEXT NOT NULL,                 -- 'challenger' | 'champion' | 'retired'
  regime       TEXT NOT NULL DEFAULT 'global',
  metrics_json TEXT,
  created_ts_ms INTEGER NOT NULL,
  note         TEXT,
  PRIMARY KEY(model_name, model_kind, model_ts_ms, regime, stage, created_ts_ms)
);

CREATE INDEX IF NOT EXISTS idx_model_registry_name_stage_regime_created
  ON model_registry(model_name, stage, regime, created_ts_ms);

CREATE INDEX IF NOT EXISTS idx_model_registry_regime_stage_created
  ON model_registry(regime, stage, created_ts_ms);

CREATE INDEX IF NOT EXISTS idx_model_registry_created
  ON model_registry(created_ts_ms);

-- Ensure only one champion per (model_name, regime)
CREATE UNIQUE INDEX IF NOT EXISTS idx_model_registry_unique_champion
  ON model_registry(model_name, regime)
  WHERE stage='champion';
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def init_model_registry(con=None) -> None:
    """
    Ensure DB initialized + registry schema/indexes exist.
    """
    _init_db()
    close = False
    if con is None:
        con = _connect()
        close = True
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        if close:
            con.close()


def register_model(
    *,
    model_name: str,
    model_kind: str,
    model_ts_ms: int,
    stage: str,
    metrics: Dict[str, Any],
    note: Optional[str] = None,
    regime: Optional[str] = None,
    key: Optional[str] = None,   # alias for regime
) -> None:
    """
    Insert a model record (append-only). `regime` is preferred. `key` accepted as alias.
    """
    reg = str(regime if regime is not None else (key if key is not None else "global"))
    init_model_registry()
    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO model_registry(
              model_name, model_kind, model_ts_ms,
              stage, regime,
              metrics_json, created_ts_ms, note
            )
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                str(model_name),
                str(model_kind),
                int(model_ts_ms),
                str(stage),
                str(reg),
                json.dumps(metrics or {}, separators=(",", ":"), sort_keys=True),
                _now_ms(),
                (str(note) if note else None),
            ),
        )
        con.commit()
    finally:
        con.close()


def get_stage_latest(
    model_name: str,
    stage: str,
    *,
    regime: Optional[str] = None,
    key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Latest record for (model_name, stage, regime). Returns dict including parsed metrics.
    """
    reg = str(regime if regime is not None else (key if key is not None else "global"))
    init_model_registry()
    con = _connect()
    try:
        r = con.execute(
            """
            SELECT model_kind, model_ts_ms, metrics_json, created_ts_ms, note, regime
            FROM model_registry
            WHERE model_name=? AND stage=? AND regime=?
            ORDER BY created_ts_ms DESC
            LIMIT 1
            """,
            (str(model_name), str(stage), str(reg)),
        ).fetchone()
        if not r:
            return None
        mk, mts, mj, cts, note, rg = r
        out = {
            "model_name": str(model_name),
            "model_kind": str(mk),
            "model_ts_ms": int(mts or 0),
            "metrics": json.loads(mj or "{}"),
            "created_ts_ms": int(cts or 0),
            "note": note,
            "stage": str(stage),
            "regime": str(rg or "global"),
        }
        # convenience: flatten common metrics for legacy callers (guards.py reads rmse)
        try:
            if isinstance(out["metrics"], dict):
                for k2, v2 in out["metrics"].items():
                    if k2 not in out:
                        out[k2] = v2
        except Exception:
            pass
        return out
    finally:
        con.close()


def list_recent(
    model_name: str,
    limit: int = 50,
    *,
    regime: Optional[str] = None,
    key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    limit = max(1, min(500, int(limit or 50)))
    reg = regime if regime is not None else key
    init_model_registry()
    con = _connect()
    try:
        if reg is None:
            rows = con.execute(
                """
                SELECT model_kind, model_ts_ms, stage, regime, metrics_json, created_ts_ms, note
                FROM model_registry
                WHERE model_name=?
                ORDER BY created_ts_ms DESC
                LIMIT ?
                """,
                (str(model_name), int(limit)),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT model_kind, model_ts_ms, stage, regime, metrics_json, created_ts_ms, note
                FROM model_registry
                WHERE model_name=? AND regime=?
                ORDER BY created_ts_ms DESC
                LIMIT ?
                """,
                (str(model_name), str(reg), int(limit)),
            ).fetchall()

        out: List[Dict[str, Any]] = []
        for mk, mts, st, rg, mj, cts, note in rows or []:
            rec = {
                "model_name": str(model_name),
                "model_kind": str(mk),
                "model_ts_ms": int(mts or 0),
                "stage": str(st),
                "regime": str(rg or "global"),
                "metrics": json.loads(mj or "{}"),
                "created_ts_ms": int(cts or 0),
                "note": note,
            }
            out.append(rec)
        return out
    finally:
        con.close()


def promote_to_champion(
    model_name: str,
    a: Union[str, None],
    b: Optional[int] = None,
    *,
    regime: Optional[str] = None,
    key: Optional[str] = None,
) -> Union[None, Tuple[Optional[str], Optional[int]]]:
    """
    Supported call patterns:

    1) promote_to_champion(model_name, to_kind, to_ts_ms, regime='global')
       -> Returns (from_kind, from_ts_ms) of previous champion (or (None,None)).

    2) promote_to_champion(model_name, regime_string)
       -> Promotes most recent challenger for that regime_string to champion.
          Returns None.
    """
    init_model_registry()

    # Pattern 2
    if b is None and isinstance(a, str) and (regime is None) and (key is None):
        reg = str(a or "global")
        con = _connect()
        try:
            row = con.execute(
                """
                SELECT model_kind, model_ts_ms
                FROM model_registry
                WHERE model_name=? AND regime=? AND stage='challenger'
                ORDER BY created_ts_ms DESC
                LIMIT 1
                """,
                (str(model_name), str(reg)),
            ).fetchone()
            if not row:
                raise RuntimeError(f"cannot promote missing challenger model={model_name} regime={reg}")
            to_kind = str(row[0])
            to_ts = int(row[1])

            con.execute("BEGIN IMMEDIATE;")
            con.execute(
                """
                UPDATE model_registry
                SET stage='retired'
                WHERE model_name=? AND regime=? AND stage='champion'
                """,
                (str(model_name), str(reg)),
            )
            con.execute(
                """
                INSERT INTO model_registry(
                  model_name, model_kind, model_ts_ms,
                  stage, regime,
                  metrics_json, created_ts_ms, note
                )
                SELECT model_name, model_kind, model_ts_ms,
                       'champion', regime,
                       metrics_json, ?, note
                FROM model_registry
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                ORDER BY created_ts_ms DESC
                LIMIT 1
                """,
                (_now_ms(), str(model_name), str(to_kind), int(to_ts), str(reg)),
            )
            con.execute("COMMIT;")
            logging.info("PROMOTED champion model=%s regime=%s kind=%s ts=%s", model_name, reg, to_kind, to_ts)
            return None
        except Exception:
            try:
                con.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            con.close()

    # Pattern 1
    to_kind = str(a) if a is not None else ""
    if b is None:
        raise TypeError("promote_to_champion(model_name, to_kind, to_ts_ms[, regime=...]) missing to_ts_ms")
    to_ts_ms = int(b)
    reg = str(regime if regime is not None else (key if key is not None else "global"))

    con = _connect()
    try:
        prev = con.execute(
            """
            SELECT model_kind, model_ts_ms
            FROM model_registry
            WHERE model_name=? AND regime=? AND stage='champion'
            ORDER BY created_ts_ms DESC
            LIMIT 1
            """,
            (str(model_name), str(reg)),
        ).fetchone()

        from_kind = prev[0] if prev else None
        from_ts = int(prev[1]) if prev and prev[1] is not None else None

        exists = con.execute(
            """
            SELECT 1
            FROM model_registry
            WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
            LIMIT 1
            """,
            (str(model_name), str(to_kind), int(to_ts_ms), str(reg)),
        ).fetchone()
        if not exists:
            raise RuntimeError(
                f"cannot promote missing model record model={model_name} regime={reg} kind={to_kind} ts={to_ts_ms}"
            )

        con.execute("BEGIN IMMEDIATE;")
        con.execute(
            """
            UPDATE model_registry
            SET stage='retired'
            WHERE model_name=? AND regime=? AND stage='champion'
            """,
            (str(model_name), str(reg)),
        )
        con.execute(
            """
            INSERT INTO model_registry(
              model_name, model_kind, model_ts_ms,
              stage, regime,
              metrics_json, created_ts_ms, note
            )
            SELECT model_name, model_kind, model_ts_ms,
                   'champion', regime,
                   metrics_json, ?, note
            FROM model_registry
            WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
            ORDER BY created_ts_ms DESC
            LIMIT 1
            """,
            (_now_ms(), str(model_name), str(to_kind), int(to_ts_ms), str(reg)),
        )
        con.execute("COMMIT;")

        logging.info("PROMOTED champion model=%s regime=%s kind=%s ts=%s", model_name, reg, to_kind, to_ts_ms)
        return (from_kind, from_ts)
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        con.close()


def rollback_champion(
    model_name: str,
    *,
    regime: Optional[str] = None,
    key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Rollback champion to most recent retired model for (model_name, regime).
    Returns new champion record or None.
    """
    init_model_registry()
    reg = str(regime if regime is not None else (key if key is not None else "global"))

    con = _connect()
    try:
        cur = con.execute(
            """
            SELECT model_kind, model_ts_ms
            FROM model_registry
            WHERE model_name=? AND regime=? AND stage='retired'
            ORDER BY created_ts_ms DESC
            LIMIT 1
            """,
            (str(model_name), str(reg)),
        ).fetchone()
        if not cur:
            return None

        to_kind = str(cur[0])
        to_ts = int(cur[1])

        con.execute("BEGIN IMMEDIATE;")
        con.execute(
            """
            UPDATE model_registry
            SET stage='retired'
            WHERE model_name=? AND regime=? AND stage='champion'
            """,
            (str(model_name), str(reg)),
        )
        con.execute(
            """
            INSERT INTO model_registry(
              model_name, model_kind, model_ts_ms,
              stage, regime,
              metrics_json, created_ts_ms, note
            )
            SELECT model_name, model_kind, model_ts_ms,
                   'champion', regime,
                   metrics_json, ?, note
            FROM model_registry
            WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
            ORDER BY created_ts_ms DESC
            LIMIT 1
            """,
            (_now_ms(), str(model_name), str(to_kind), int(to_ts), str(reg)),
        )
        con.execute("COMMIT;")
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        con.close()

    return get_stage_latest(model_name, "champion", regime=reg)
