# dev_core/model_registry.py
import time
import json
import os
import logging
from typing import Optional, Dict, Any, List, Tuple, Union

from engine.storage import connect, init_db, _has_column

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [model_registry] %(message)s",
)

# Canonical schema is created in dev_core/storage.py:
# model_registry(
#   model_name TEXT,
#   model_kind TEXT,
#   model_ts_ms INTEGER,
#   stage TEXT,
#   regime TEXT DEFAULT 'global',
#   metrics_json TEXT,
#   created_ts_ms INTEGER,
#   note TEXT,
#   PRIMARY KEY(model_name, model_kind, model_ts_ms)
# )

INDEXES = """
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
    Ensure registry indexes exist (table is created by init_db()).
    Accepts optional SQLite connection. If provided, does NOT close it.
    """
    init_db()

    close = False
    if con is None:
        con = connect()
        close = True

    try:
        con.executescript(INDEXES)
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
    key: Optional[str] = None,
    regime: Optional[str] = None,
) -> None:
    """
    Register a model into the canonical model_registry table.

    Compatibility:
      - `regime` is preferred.
      - `key` is accepted as alias for regime.
    """
    init_model_registry()
    con = connect()
    try:
        reg = str(regime if regime is not None else (key if key is not None else "global"))
        

        con.execute(
            """
            INSERT OR REPLACE INTO model_registry(
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
    Latest model record for (model_name, stage, regime).
    Default regime='global'.
    """
    init_model_registry()
    reg = str(regime if regime is not None else (key if key is not None else "global"))

    con = connect()
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
        return {
            "model_name": str(model_name),
            "model_kind": str(r[0]),
            "model_ts_ms": int(r[1] or 0),
            "metrics": json.loads(r[2] or "{}"),
            "created_ts_ms": int(r[3] or 0),
            "note": r[4],
            "stage": str(stage),
            "regime": str(r[5] or "global"),
        }
    finally:
        con.close()


def list_recent(
    model_name: str,
    limit: int = 50,
    *,
    regime: Optional[str] = None,
    key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    List recent registry entries for a model (optionally filtered by regime).
    """
    init_model_registry()
    limit = max(1, min(500, int(limit or 50)))
    reg = regime if regime is not None else key

    con = connect()
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
            out.append(
                {
                    "model_name": str(model_name),
                    "model_kind": str(mk),
                    "model_ts_ms": int(mts or 0),
                    "stage": str(st),
                    "regime": str(rg or "global"),
                    "metrics": json.loads(mj or "{}"),
                    "created_ts_ms": int(cts or 0),
                    "note": note,
                }
            )
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
    Promote challenger -> champion in canonical table.

    Supported call patterns:

    1) promote_to_champion(model_name, to_kind, to_ts_ms, regime='global')
       -> Returns (from_kind, from_ts_ms) of previous champion (or (None,None)).

    2) promote_to_champion(model_name, regime_string)
       -> Promotes most recent challenger for that regime_string to champion.
          Returns None.
    """
    init_model_registry()

    # Pattern 2: called as (model_name, regime_string)
    if b is None and isinstance(a, str) and (regime is None) and (key is None):
        reg = str(a or "global")
        con = connect()
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
                UPDATE model_registry
                SET stage='champion'
                WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
                """,
                (str(model_name), str(to_kind), int(to_ts), str(reg)),
            )

            con.execute("COMMIT;")
            logging.info("PROMOTED champion model=%s regime=%s kind=%s ts=%s", model_name, reg, to_kind, to_ts)
            return None
        except Exception as e:
            try:
                con.execute("ROLLBACK;")
            except Exception:
                pass
            logging.error("PROMOTION_FAILED model=%s err=%r", model_name, e)
            raise
        finally:
            con.close()

    # Pattern 1
    to_kind = str(a) if a is not None else ""
    if b is None:
        raise TypeError("promote_to_champion(model_name, to_kind, to_ts_ms[, regime=...]) missing to_ts_ms")
    to_ts_ms = int(b)
    reg = str(regime if regime is not None else (key if key is not None else "global"))

    con = connect()
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

        row = con.execute(
            """
            SELECT 1
            FROM model_registry
            WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
            LIMIT 1
            """,
            (str(model_name), str(to_kind), int(to_ts_ms), str(reg)),
        ).fetchone()
        if not row:
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
            UPDATE model_registry
            SET stage='champion'
            WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
            """,
            (str(model_name), str(to_kind), int(to_ts_ms), str(reg)),
        )
        con.execute("COMMIT;")

        logging.info("PROMOTED champion model=%s regime=%s kind=%s ts=%s", model_name, reg, to_kind, to_ts_ms)
        return (from_kind, from_ts)
    except Exception as e:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        logging.error("PROMOTION_FAILED model=%s regime=%s kind=%s ts=%s err=%r", model_name, reg, to_kind, to_ts_ms, e)
        raise
    finally:
        con.close()


def rollback_champion(model_name: str, *, regime: Optional[str] = None, key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Rollback champion to most recent retired model for (model_name, regime).
    Returns new champion record or None.
    """
    init_model_registry()
    reg = str(regime if regime is not None else (key if key is not None else "global"))

    con = connect()
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
            UPDATE model_registry
            SET stage='champion'
            WHERE model_name=? AND model_kind=? AND model_ts_ms=? AND regime=?
            """,
            (str(model_name), str(to_kind), int(to_ts), str(reg)),
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
