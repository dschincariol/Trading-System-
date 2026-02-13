"""
A.6 Trainable Temporal Predictor (shadow-mode only).

Predicts impact_z from a sequence of recent event embeddings (plus dt feature).
Models are stored in SQLite table: temporal_models.

Shadow-mode: process_events.py may log predictions to temporal_predictions but does NOT
affect alerts unless you later wire a separate live flag.
"""

import io
import os
import math
import json
import time
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn

# Shadow-mode safety: default to CPU unless explicitly allowed
if os.environ.get("TEMPORAL_USE_CUDA", "0") != "1":
    try:
        torch.set_default_device("cpu")
    except Exception:
        pass

# Optional: cap CPU threads (helps overall system stability under load)
try:
    _threads = int(os.environ.get("TORCH_NUM_THREADS", "0"))
    if _threads > 0:
        torch.set_num_threads(_threads)
except Exception:
    pass

from dev_core.storage import connect
from dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from dev_core.asset_map import asset_class_for_symbol

# -------------            -- ------------------------------------------------------
# Flags
# -------------            -- ------------------------------------------------------
TEMPORAL_SHADOW = os.environ.get("TEMPORAL_PRED_SHADOW", "0") == "1"

# Sequence length used by predictor (must match training)
TEMPORAL_SEQ_LEN = int(os.environ.get("TEMPORAL_SEQ_LEN", "6"))

# Confidence scale: conf_raw = 1 - exp(-n / k)
TEMPORAL_CONF_K = float(os.environ.get("TEMPORAL_CONF_K", "75.0"))

# Use temporal embeddings table instead of base embeddings (if populated)
USE_TEMPORAL_EMB = os.environ.get("USE_TEMPORAL_EMB_TABLE", "0") == "1"

# -------------            -- ------------------------------------------------------
# Determinism / performance knobs
# -------------            -- ------------------------------------------------------
_TORCH_SEED = 42
torch.manual_seed(_TORCH_SEED)

# Default: favor performance (especially on RTX PRO 2000) unless explicitly requested.
_DET = os.environ.get("TORCH_DETERMINISTIC", "0") == "1"
try:
    torch.use_deterministic_algorithms(_DET)
except Exception:
    pass

try:
    torch.backends.cudnn.deterministic = _DET
    torch.backends.cudnn.benchmark = (not _DET) and (os.environ.get("CUDNN_BENCHMARK", "1") == "1")
except Exception:
    pass

# TF32 is a big win on newer RTX cards for matmul-heavy workloads.
try:
    torch.set_float32_matmul_precision(os.environ.get("TORCH_MATMUL_PRECISION", "high"))
except Exception:
    pass
try:
    torch.backends.cuda.matmul.allow_tf32 = os.environ.get("TORCH_ALLOW_TF32", "1") == "1"
except Exception:
    pass
try:
    torch.backends.cudnn.allow_tf32 = os.environ.get("CUDNN_ALLOW_TF32", "1") == "1"
except Exception:
    pass


# -------------            -- ------------------------------------------------------
# Blob format
# -------------            -- ------------------------------------------------------
_TMAGIC = b"TMP1"

# -------------            -- ------------------------------------------------------
# Shadow logging schema (A.7)
# -------------            -- ------------------------------------------------------
# Shadow-mode only: write predictions for later evaluation.
# This DOES NOT affect alerts/execution.
_TEMPORAL_SHADOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS temporal_predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  event_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  pred_z REAL NOT NULL,
  conf_raw REAL NOT NULL,
  model_key_type TEXT NOT NULL,
  model_key TEXT NOT NULL,
  model_ts_ms INTEGER NOT NULL,
  model_n INTEGER NOT NULL,
  explain_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_temporal_predictions_eid
  ON temporal_predictions(event_id);

CREATE INDEX IF NOT EXISTS idx_temporal_predictions_sym_h
  ON temporal_predictions(symbol, horizon_s, ts_ms);

CREATE INDEX IF NOT EXISTS idx_temporal_predictions_ts
  ON temporal_predictions(ts_ms);
"""

_SHADOW_DB_READY = False


def init_temporal_shadow_db(con) -> None:
    global _SHADOW_DB_READY
    if _SHADOW_DB_READY:
        return
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=3000;")
        con.executescript(_TEMPORAL_SHADOW_SCHEMA)
        con.commit()
        _SHADOW_DB_READY = True
    except Exception:
        # Shadow-only: never break caller
        pass


def log_temporal_shadow_prediction(
    con,
    event_id: int,
    ts_ms: int,
    symbol: str,
    horizon_s: int,
    pred_z: float,
    conf_raw: float,
    explain: Dict[str, Any],
) -> None:
    """
    Inserts one row into temporal_predictions (shadow-only).
    Safe no-op on any error.
    """
    try:
        init_temporal_shadow_db(con)

        sym_u = str(symbol or "").upper().strip()
        if not sym_u:
            return

        e = dict(explain or {})
        model_key_type = str(e.get("model_key_type") or "")
        model_key = str(e.get("model_key") or "")
        model_ts_ms = int(e.get("model_ts_ms") or 0)
        model_n = int(e.get("model_n") or 0)

        payload = json.dumps(e, separators=(",", ":"), sort_keys=True)

        

        con.execute(
            """
            INSERT INTO temporal_predictions(
              ts_ms, event_id, symbol, horizon_s,
              pred_z, conf_raw,
              model_key_type, model_key, model_ts_ms, model_n,
              explain_json
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(ts_ms),
                int(event_id),
                sym_u,
                int(horizon_s),
                float(pred_z),
                float(conf_raw),
                model_key_type,
                model_key,
                int(model_ts_ms),
                int(model_n),
                payload,
            ),
        )
    except Exception:
        return

# Cache models to avoid torch.load + state rebuild on every prediction
# Key: (key_type, key, horizon_s, model_ts_ms)
_MODEL_CACHE: Dict[Tuple[str, str, int, int], Tuple[nn.Module, Dict[str, Any]]] = {}
_MODEL_CACHE_MAX = int(os.environ.get("TEMPORAL_MODEL_CACHE_MAX", "256"))


def _cache_put(cache_key: Tuple[str, str, int, int], model: nn.Module, payload: Dict[str, Any]) -> None:
    try:
        max_n = int(_MODEL_CACHE_MAX)
        if len(_MODEL_CACHE) >= max_n:
            # Fast eviction: drop ~25% of entries
            for i, k in enumerate(list(_MODEL_CACHE.keys())):
                if i % 4 == 0:
                    _MODEL_CACHE.pop(k, None)
        _MODEL_CACHE[cache_key] = (model, payload)
    except Exception:
        pass

def _cache_get(cache_key: Tuple[str, str, int, int]) -> Optional[Tuple[nn.Module, Dict[str, Any]]]:
    try:
        return _MODEL_CACHE.get(cache_key)
    except Exception:
        return None


class _TemporalMLP(nn.Module):
    def __init__(self, input_dim: int, hidden: List[int]):
        super().__init__()
        dims = [int(input_dim)] + [int(h) for h in (hidden or [])] + [1]
        layers: List[nn.Module] = []
        for i in range(len(dims) - 2):
            layers.append(nn.Linear(dims[i], dims[i + 1]))
            layers.append(nn.ReLU())
        layers.append(nn.Linear(dims[-2], dims[-1]))
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)


def _deserialize_payload(blob: bytes) -> Dict[str, Any]:
    if not blob.startswith(_TMAGIC):
        raise ValueError("not temporal blob")
    raw = blob[len(_TMAGIC):]
    bio = io.BytesIO(raw)

    # torch.load signature differs across versions; keep compatibility
    try:
        payload = torch.load(bio, map_location="cpu", weights_only=False)
    except TypeError:
        bio.seek(0)
        payload = torch.load(bio, map_location="cpu")

    if not isinstance(payload, dict):
        raise ValueError("invalid temporal payload")
    return payload


def _conf_from_n(n_train: int, k: float) -> float:
    try:
        n = max(0, int(n_train))
        kk = float(k)
        if kk <= 1e-9:
            return 0.0
        return float(max(0.0, min(1.0, 1.0 - math.exp(-float(n) / kk))))
    except Exception:
        return 0.0


def _embedding_table() -> str:
    return "event_embeddings_seq" if USE_TEMPORAL_EMB else "event_embeddings"


def _load_embedding(con, event_id: int) -> Optional[np.ndarray]:
    row = con.execute(
        f"SELECT vec FROM {_embedding_table()} WHERE event_id=?",
        (int(event_id),),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return np.frombuffer(row[0], dtype=np.float32)


def _load_recent_event_ids(con, ts_ms: int, seq_len: int) -> List[Tuple[int, int]]:
    """
    Returns list of (event_id, ts_ms) for most recent events with embeddings
    at or before ts_ms, ordered oldest->newest, length <= seq_len.
    """
    rows = con.execute(
        f"""
        SELECT e.id, e.ts_ms
        FROM events e
        JOIN {_embedding_table()} emb ON emb.event_id = e.id
        WHERE e.ts_ms <= ?
        ORDER BY e.ts_ms DESC
        LIMIT ?
        """,
        (int(ts_ms), int(seq_len)),
    ).fetchall()

    out = [(int(r[0]), int(r[1])) for r in (rows or [])]
    out.reverse()
    return out


def build_sequence_vector(con, ts_ms: int, seq_len: int) -> Optional[Tuple[np.ndarray, int]]:
    """
    Builds flattened sequence feature:
      for each step i: [embedding_dim floats..., dt_seconds]
    dt_seconds = (ts_i - ts_{i-1})/1000 for i>0 else 0
    Returns (flat_vec, embed_dim).
    """
    ids = _load_recent_event_ids(con, int(ts_ms), int(seq_len))
    if len(ids) < int(seq_len):
        return None

    vecs: List[np.ndarray] = []
    prev_ts: Optional[int] = None
    embed_dim: Optional[int] = None

    for (eid, ets) in ids:
        v = _load_embedding(con, int(eid))
        if v is None:
            return None

        if embed_dim is None:
            embed_dim = int(v.shape[0])

        if int(v.shape[0]) != int(embed_dim):
            return None

        if prev_ts is None:
            dt_s = 0.0
        else:
            dt_s = float(max(0, int(ets) - int(prev_ts))) / 1000.0
        prev_ts = int(ets)

        vecs.append(
            np.concatenate(
                [v.astype(np.float32, copy=False), np.asarray([dt_s], dtype=np.float32)]
            )
        )

    flat = np.concatenate(vecs).astype(np.float32, copy=False)
    return flat, int(embed_dim or 0)


def _get_model_row(con, key_type: str, key: str, horizon_s: int):
    return con.execute(
        """
        SELECT ts_ms, n, embed_dim, seq_len, model_kind, model_blob
        FROM temporal_models
        WHERE key_type=? AND key=? AND horizon_s=?
        """,
        (str(key_type), str(key), int(horizon_s)),
    ).fetchone()


def _predict_one(con, symbol: str, horizon_s: int, x: np.ndarray) -> Optional[Tuple[float, float, Dict[str, Any]]]:
    """
    Returns (pred_z, conf, explain) or None
    Tries: symbol -> class -> global
    """
    sym_u = str(symbol).upper().strip()
    if not sym_u:
        return None

    tries: List[Tuple[str, str]] = [("symbol", sym_u)]
    try:
        cls = asset_class_for_symbol(sym_u)
    except Exception:
        cls = None
    if cls and str(cls).upper() != "UNKNOWN":
        tries.append(("class", str(cls).upper()))
    tries.append(("global", "ALL"))

    x = np.asarray(x, dtype=np.float32).reshape(-1)
    if x.size == 0 or not np.all(np.isfinite(x)):
        return None

    for kt, k in tries:
        row = _get_model_row(con, kt, k, int(horizon_s))
        if not row:
            continue

        ts_ms, n, embed_dim, seq_len, model_kind, blob = row
        if blob is None:
            continue

        ts_i = int(ts_ms or 0)
        cache_key = (str(kt), str(k), int(horizon_s), ts_i)

        try:
            cached = _cache_get(cache_key)
            if cached is not None:
                model, payload = cached
            else:
                payload = _deserialize_payload(bytes(blob))

                input_dim = int(payload.get("input_dim") or 0)
                payload_seq_len = int(payload.get("seq_len") or 0)

                # Must match predictor seq length if provided
                if payload_seq_len > 0 and int(payload_seq_len) != int(TEMPORAL_SEQ_LEN):
                    continue
                if input_dim <= 0:
                    continue

                hidden = payload.get("hidden") or [256, 128]
                model = _TemporalMLP(input_dim=int(input_dim), hidden=[int(h) for h in hidden])

                sd = payload.get("state_dict")
                if not isinstance(sd, dict):
                    continue
                model.load_state_dict(sd)
                model.eval()

                _cache_put(cache_key, model, payload)

            input_dim = int(payload.get("input_dim") or 0)
            if input_dim <= 0:
                continue
            if int(x.shape[0]) != int(input_dim):
                continue

            x_mean = np.asarray(payload.get("x_mean"), dtype=np.float32).reshape(-1)
            x_std = np.asarray(payload.get("x_std"), dtype=np.float32).reshape(-1)
            if x_mean.shape[0] != int(input_dim) or x_std.shape[0] != int(input_dim):
                continue
            if not np.all(np.isfinite(x_mean)) or not np.all(np.isfinite(x_std)):
                continue

            x_std = np.where(x_std < 1e-6, 1.0, x_std).astype(np.float32)

            y_mean = float(payload.get("y_mean") or 0.0)
            y_std = float(payload.get("y_std") or 1.0)
            if not np.isfinite(y_mean):
                y_mean = 0.0
            if not np.isfinite(y_std) or abs(float(y_std)) < 1e-6:
                y_std = 1.0

            xn = (x - x_mean) / x_std
            if not np.all(np.isfinite(xn)):
                continue

            with torch.no_grad():
                pred_n = float(model(torch.from_numpy(xn.reshape(1, -1))).item())
            pred = float(pred_n * y_std + y_mean)

            conf = _conf_from_n(int(n or 0), float(TEMPORAL_CONF_K))

            explain: Dict[str, Any] = {
                "model": "temporal_predictor",
                "model_kind": str(model_kind or "temporal_mlp"),
                "model_key_type": str(kt),
                "model_key": str(k),
                "model_ts_ms": ts_i,
                "model_n": int(n or 0),
                "seq_len": int(seq_len or 0),
                "embed_dim": int(embed_dim or 0),
                "conf_raw": float(conf),
            }
            return float(pred), float(conf), explain
        except Exception:
            continue

    return None

def predict_temporal_shadow(
    con,
    ts_ms: int,
    symbols: List[str],
    horizons: List[int],
    seq_len: int = TEMPORAL_SEQ_LEN,
) -> Optional[Dict[Tuple[str, int], Tuple[float, float, Dict[str, Any]]]]:
    """
    Builds sequence features once per timestamp and predicts for requested symbols/horizons.
    Returns map[(sym,h)] = (z, conf, explain) or None if unavailable.
    """
    if not TEMPORAL_SHADOW:
        return None

    built = build_sequence_vector(con, ts_ms=int(ts_ms), seq_len=int(seq_len))
    if not built:
        return None
    x, _embed_dim = built

    out: Dict[Tuple[str, int], Tuple[float, float, Dict[str, Any]]] = {}

    hs: List[int] = []
    for h in (horizons or []):
        try:
            hi = int(h)
            if hi > 0:
                hs.append(hi)
        except Exception:
            continue

    syms: List[str] = []
    for s in (symbols or []):
        su = str(s or "").upper().strip()
        if su:
            syms.append(su)

    for hi in hs:
        for su in syms:
            r = _predict_one(con, su, int(hi), x)
            if r is None:
                continue
            z, conf, explain = r
            out[(su, int(hi))] = (float(z), float(conf), explain)

    return out


def predict_temporal_shadow_open(
    ts_ms: int,
    symbols: List[str],
    horizons: List[int],
    seq_len: int = TEMPORAL_SEQ_LEN,
) -> Optional[Dict[Tuple[str, int], Tuple[float, float, Dict[str, Any]]]]:
    con = connect()
    try:
        return predict_temporal_shadow(
            con,
            ts_ms=int(ts_ms),
            symbols=symbols,
            horizons=horizons,
            seq_len=int(seq_len),
        )
    finally:
        try:
            con.close()
        except Exception:
            pass


def predict_temporal_shadow_for_event(
    con,
    event_id: int,
    ts_ms: int,
    symbols: List[str],
    horizons: List[int],
    seq_len: int = TEMPORAL_SEQ_LEN,
) -> Optional[Dict[Tuple[str, int], Tuple[float, float, Dict[str, Any]]]]:
    """
    A.7 integration helper:
      - runs shadow predictions
      - optionally logs to temporal_predictions if TEMPORAL_SHADOW_LOG=1

    Does NOT affect alerts/execution.
    """
    # Ensure shadow schema exists early (safe no-op if already created)
    try:
        init_temporal_shadow_db(con)
    except Exception:
        pass

    out = predict_temporal_shadow(
        con,
        ts_ms=int(ts_ms),
        symbols=symbols,
        horizons=horizons,
        seq_len=int(seq_len),
    )

    # If shadow disabled, do not affect caller, but still allow logging if enabled
    if not out:
        return out


    if os.environ.get("TEMPORAL_SHADOW_LOG", "0") == "1":
        for (sym, h), (z, conf, explain) in out.items():
            log_temporal_shadow_prediction(
                con,
                event_id=int(event_id),
                ts_ms=int(ts_ms),
                symbol=str(sym),
                horizon_s=int(h),
                pred_z=float(z),
                conf_raw=float(conf),
                explain=explain,
            )
        try:
            con.commit()
        except Exception:
            pass

    return out

__all__ = [
    "predict_temporal_shadow",
    "predict_temporal_shadow_open",
    "predict_temporal_shadow_for_event",
    "build_sequence_vector",
]
