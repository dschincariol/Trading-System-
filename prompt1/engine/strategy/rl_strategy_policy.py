# dev_core/rl_strategy_policy.py
"""
RL strategy policy infrastructure (B2/C2 fusion, Milestone 1).

- Stores a lightweight linear policy in SQLite (similar spirit to embed_models2).
- Provides:
    init_rl_policy_db()
    load_policy()
    predict_strategy(features)
    log_decision()

By default this module is used in SHADOW mode only by strategy_selector.
"""

import io
import json
import time
import sqlite3
from typing import Dict, Tuple, Optional

import numpy as np

from engine.storage import connect
from engine.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rl_strategy_policy_models (
  policy_name TEXT PRIMARY KEY,      -- e.g. 'v1'
  ts_ms INTEGER NOT NULL,
  n INTEGER NOT NULL,
  dim INTEGER NOT NULL,
  model_blob BLOB NOT NULL,          -- float32[dim] weights + float32 bias
  meta_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rl_strategy_policy_models_ts ON rl_strategy_policy_models(ts_ms);

CREATE TABLE IF NOT EXISTS rl_strategy_policy_decisions (
  ts_ms INTEGER NOT NULL,
  rule_choice TEXT NOT NULL,
  rl_choice TEXT NOT NULL,
  used_choice TEXT NOT NULL,
  rl_score REAL NOT NULL,            -- signed score (positive favors conservative)
  features_json TEXT NOT NULL,
  context_json TEXT NOT NULL,
  PRIMARY KEY (ts_ms)
);

CREATE INDEX IF NOT EXISTS idx_rl_strategy_policy_decisions_ts ON rl_strategy_policy_decisions(ts_ms);
"""

DEFAULT_POLICY_NAME = "v1"

def init_rl_policy_db() -> None:
    con = connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()

def _serialize_linear(weights: np.ndarray, bias: float) -> bytes:
    w = np.asarray(weights, dtype=np.float32).reshape(-1)
    b = np.asarray([float(bias)], dtype=np.float32)
    buf = io.BytesIO()
    buf.write(np.int32(w.shape[0]).tobytes())
    buf.write(w.tobytes())
    buf.write(b.tobytes())
    return buf.getvalue()

def _deserialize_linear(blob: bytes) -> Tuple[np.ndarray, float]:
    b = memoryview(blob)
    dim = int(np.frombuffer(b[:4], dtype=np.int32)[0])
    off = 4
    w = np.frombuffer(b[off:off + dim * 4], dtype=np.float32).copy()
    off += dim * 4
    bias = float(np.frombuffer(b[off:off + 4], dtype=np.float32)[0])
    return w, bias

def upsert_policy(policy_name: str, weights: np.ndarray, bias: float, n: int, feature_names) -> None:
    """
    Stores policy in SQLite. Idempotent by policy_name.
    """
    init_rl_policy_db()
    now_ms = int(time.time() * 1000)
    w = np.asarray(weights, dtype=np.float32).reshape(-1)
    blob = _serialize_linear(w, float(bias))
    meta = {
        "feature_names": list(feature_names or []),
        "type": "linear",
    }

    con = connect()
    try:
        

        con.execute(
            """
            INSERT INTO rl_strategy_policy_models(policy_name, ts_ms, n, dim, model_blob, meta_json)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(policy_name) DO UPDATE SET
              ts_ms=excluded.ts_ms,
              n=excluded.n,
              dim=excluded.dim,
              model_blob=excluded.model_blob,
              meta_json=excluded.meta_json
            """,
            (
                str(policy_name),
                int(now_ms),
                int(n),
                int(w.shape[0]),
                sqlite3.Binary(blob),
                json.dumps(meta, separators=(",", ":"), sort_keys=True),
            ),
        )
        con.commit()
    finally:
        con.close()

def load_policy(policy_name: str = DEFAULT_POLICY_NAME) -> Optional[Dict]:
    init_rl_policy_db()
    con = connect()
    try:
        row = con.execute(
            """
            SELECT ts_ms, n, dim, model_blob, meta_json
            FROM rl_strategy_policy_models
            WHERE policy_name=?
            """,
            (str(policy_name),),
        ).fetchone()
        if not row:
            return None
        ts_ms, n, dim, blob, meta_json = row
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except Exception:
            meta = {}
        w, bias = _deserialize_linear(blob)
        return {
            "policy_name": str(policy_name),
            "ts_ms": int(ts_ms),
            "n": int(n),
            "dim": int(dim),
            "weights": w,
            "bias": float(bias),
            "feature_names": list(meta.get("feature_names") or []),
        }
    finally:
        con.close()

def _build_feature_vector(features: Dict, feature_names) -> np.ndarray:
    xs = []
    for k in (feature_names or []):
        try:
            xs.append(float(features.get(k, 0.0) or 0.0))
        except Exception:
            xs.append(0.0)
    return np.asarray(xs, dtype=np.float32)

def predict_strategy(
    features: Dict,
    policy: Optional[Dict],
    *,
    threshold: float = 0.0
) -> Tuple[str, float]:
    """
    Returns (choice, score).
    score > 0 => favors 'conservative'
    score <= 0 => favors 'baseline'
    """
    if not policy:
        # Safe deterministic fallback heuristic (shadow-only by default):
        # if drawdown is large negative, prefer conservative.
        dd = float(features.get("prev_drawdown", 0.0) or 0.0)
        score = float(-dd)  # more drawdown => higher score
        choice = "conservative" if score > float(threshold) else "baseline"
        return choice, float(score)

    x = _build_feature_vector(features, policy.get("feature_names") or [])
    w = policy["weights"]
    bias = float(policy["bias"])
    if x.shape[0] != w.shape[0]:
        # shape mismatch -> fallback
        return "baseline", 0.0

    score = float(np.dot(w, x) + bias)
    choice = "conservative" if score > float(threshold) else "baseline"
    return choice, float(score)

def log_decision(
    *,
    ts_ms: int,
    rule_choice: str,
    rl_choice: str,
    used_choice: str,
    rl_score: float,
    features: Dict,
    context: Dict,
) -> None:
    init_rl_policy_db()
    con = connect()
    try:
        

        con.execute(
            """
            INSERT OR REPLACE INTO rl_strategy_policy_decisions(
              ts_ms, rule_choice, rl_choice, used_choice, rl_score, features_json, context_json
            )
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                int(ts_ms),
                str(rule_choice),
                str(rl_choice),
                str(used_choice),
                float(rl_score),
                json.dumps(features or {}, separators=(",", ":"), sort_keys=True),
                json.dumps(context or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
        con.commit()
    finally:
        con.close()
