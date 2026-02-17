# dev_core/decision_log.py

import json
import time
import hashlib
import os
import logging
from typing import Optional, Dict, Any, Sequence

import numpy as np

from engine.dev_core.storage import connect, init_db


# ------            -- ------------------------------------------------------
# Hash helpers
# ------            -- ------------------------------------------------------
def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def hash_feature_vector(vec: Optional[Sequence[float]]) -> Optional[str]:
    if vec is None:
        return None
    try:
        arr = np.asarray(vec, dtype=np.float32)
        return _sha256_bytes(arr.tobytes())
    except Exception:
        return None


# ------            -- ------------------------------------------------------
# Logging
# ------            -- ------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [decision_log] %(message)s",
)


# ------            -- ------------------------------------------------------
# Schema
# ------            -- ------------------------------------------------------
def init_decision_log_db(con=None):
    close = False
    if con is None:
        con = connect()
        close = True

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_log (
          ts_ms INTEGER NOT NULL,
          event_id INTEGER NOT NULL,
          symbol TEXT NOT NULL,
          horizon_s INTEGER NOT NULL,
          predicted_z REAL NOT NULL,
          confidence REAL NOT NULL,
          model_name TEXT,
          model_kind TEXT,
          model_ts_ms INTEGER,
          features_hash TEXT,
          features_json TEXT,
          explain_json TEXT,
          extra_json TEXT
        )
        """
    )

    # audit / query helpers
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_event ON decision_log(event_id)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_symbol_h ON decision_log(symbol, horizon_s)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_model ON decision_log(model_name, model_kind)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_decision_ts ON decision_log(ts_ms)"
    )

    if close:
        con.close()


# ------            -- ------------------------------------------------------
# Write API
# ------            -- ------------------------------------------------------
def log_decision(
    *,
    event_id: int,
    symbol: str,
    horizon_s: int,
    predicted_z: float,
    confidence: float,
    model_name: str,
    model_kind: Optional[str] = None,
    model_ts_ms: Optional[int] = None,
    features_hash: Optional[str] = None,
    features_json: Optional[Dict[str, Any]] = None,
    explain_json: Optional[Dict[str, Any]] = None,
    extra_json: Optional[Dict[str, Any]] = None,
    ts_ms: Optional[int] = None,
) -> None:
    init_db()
    init_decision_log_db()

    now_ms = int(ts_ms if ts_ms is not None else time.time() * 1000)

    def _dump(x):
        if x is None:
            return None
        try:
            s = json.dumps(x, ensure_ascii=False)
            # cap to ~64KB defensively
            return s if len(s) <= 65536 else s[:65536]
        except Exception:
            return None

    con = connect()
    try:
        

        con.execute(
            """
            INSERT INTO decision_log(
              ts_ms, event_id, symbol, horizon_s,
              predicted_z, confidence,
              model_name, model_kind, model_ts_ms,
              features_hash, features_json, explain_json, extra_json
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(now_ms),
                int(event_id),
                str(symbol),
                int(horizon_s),
                float(predicted_z),
                float(confidence),
                str(model_name),
                (str(model_kind) if model_kind is not None else None),
                (int(model_ts_ms) if model_ts_ms is not None else None),
                (str(features_hash) if features_hash is not None else None),
                _dump(features_json),
                _dump(explain_json),
                _dump(extra_json),
            ),
        )
        con.commit()
    except Exception as e:
        logging.warning("log_decision failed: %r", e)
    finally:
        con.close()
