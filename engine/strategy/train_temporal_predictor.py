"""
A.6 Trainable Temporal Predictor training script (shadow-mode).

Trains temporal MLP(s): sequence_of_embeddings -> impact_z
Stores to SQLite temporal_models (+ eval to temporal_model_eval).

Usage:
  python train_temporal_predictor.py
"""

import io
import json
import os
import time
import sqlite3
import socket
from typing import Dict, List, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn

# Shadow-mode safety: default to CPU unless explicitly allowed.
# Set TEMPORAL_USE_CUDA=1 to train on RTX PRO 2000.
if os.environ.get("TEMPORAL_USE_CUDA", "0") != "1":
    torch.set_default_device("cpu")
else:
    try:
        torch.set_default_device("cuda")
    except Exception:
        pass

# Performance flags (TF32 + cuDNN benchmark when not deterministic)
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

from engine.dev_core.storage import connect, init_db, acquire_job_lock, release_job_lock
from engine.dev_core.asset_map import asset_class_for_symbol
from engine.dev_core.training_guard import training_allowed

# ------            -- ------------------------------------------------------
# Job identity
# ------            -- ------------------------------------------------------

JOB_NAME = "train_temporal_predictor"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", socket.gethostname())),
)
PID = os.getpid()

# ------            -- ------------------------------------------------------
# Constants / schema
# ------            -- ------------------------------------------------------

_TMAGIC = b"TMP1"
_TORCH_SEED = 42

_SCHEMA = """
CREATE TABLE IF NOT EXISTS temporal_models (
  key_type TEXT NOT NULL,
  key TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL,
  n INTEGER NOT NULL,
  embed_dim INTEGER NOT NULL,
  seq_len INTEGER NOT NULL,
  model_kind TEXT NOT NULL,
  model_blob BLOB NOT NULL,
  PRIMARY KEY (key_type, key, horizon_s)
);

CREATE INDEX IF NOT EXISTS idx_temporal_models_ts
  ON temporal_models(ts_ms);

CREATE TABLE IF NOT EXISTS temporal_model_eval (
  key_type TEXT NOT NULL,
  key TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  model_kind TEXT NOT NULL,
  ts_ms INTEGER NOT NULL,
  n_train INTEGER NOT NULL,
  n_eval INTEGER NOT NULL,
  rmse REAL NOT NULL,
  spearman REAL NOT NULL,
  directional_acc REAL NOT NULL,
  PRIMARY KEY (key_type, key, horizon_s, model_kind)
);
"""

# ------            -- ------------------------------------------------------
# Model
# ------            -- ------------------------------------------------------

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


# ------            -- ------------------------------------------------------
# Helpers
# ------            -- ------------------------------------------------------

def _put_provider_health(con, ts_ms: int, provider: str, ok: int, latency_ms: int, n_symbols: int, error: str = None) -> None:
    con.execute(
        """
        INSERT INTO price_provider_health(ts_ms, provider, ok, latency_ms, n_symbols, error)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(provider, ts_ms) DO UPDATE SET
          ok=excluded.ok,
          latency_ms=excluded.latency_ms,
          n_symbols=excluded.n_symbols,
          error=excluded.error
        """,
        (
            int(ts_ms),
            str(provider),
            int(ok),
            (int(latency_ms) if latency_ms is not None else None),
            int(n_symbols),
            (str(error) if error else None),
        ),
    )

def _serialize_payload(payload: Dict) -> bytes:
    buf = io.BytesIO()
    torch.save(payload, buf)
    return _TMAGIC + buf.getvalue()


def _embedding_table() -> str:
    return "event_embeddings_seq" if os.environ.get("USE_TEMPORAL_EMB_TABLE", "0") == "1" else "event_embeddings"


def _eval_predictions(y_true: np.ndarray, y_pred: np.ndarray) -> Tuple[float, float, float]:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    if y_true.size == 0:
        return 0.0, 0.0, 0.0

    rmse = float(np.sqrt(np.mean((y_true - y_pred) ** 2)))

    try:
        rt = y_true.argsort().argsort()
        rp = y_pred.argsort().argsort()
        spearman = float(np.corrcoef(rt, rp)[0, 1])
        if not np.isfinite(spearman):
            spearman = 0.0
    except Exception:
        spearman = 0.0

    try:
        # Treat tiny magnitudes as 0 to avoid noisy sign flips
        eps = 1e-9
        yt_s = np.sign(np.where(np.abs(y_true) < eps, 0.0, y_true))
        yp_s = np.sign(np.where(np.abs(y_pred) < eps, 0.0, y_pred))
        directional = float(np.mean(yt_s == yp_s))
    except Exception:
        directional = 0.0

    return rmse, spearman, directional


def _load_recent_event_ids(con, ts_ms: int, seq_len: int) -> List[Tuple[int, int]]:
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


def _load_embedding(con, event_id: int) -> Optional[np.ndarray]:
    row = con.execute(
        f"SELECT vec FROM {_embedding_table()} WHERE event_id=?",
        (int(event_id),),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return np.frombuffer(row[0], dtype=np.float32)


def _build_sequence_flat(con, ts_ms: int, seq_len: int) -> Optional[Tuple[np.ndarray, int]]:
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


def _upsert_model(
    con,
    key_type: str,
    key: str,
    horizon_s: int,
    now_ms: int,
    n: int,
    embed_dim: int,
    seq_len: int,
    kind: str,
    blob: bytes,
):
    con.execute(
        """
        INSERT INTO temporal_models(key_type, key, horizon_s, ts_ms, n, embed_dim, seq_len, model_kind, model_blob)
        VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(key_type, key, horizon_s) DO UPDATE SET
          ts_ms=excluded.ts_ms,
          n=excluded.n,
          embed_dim=excluded.embed_dim,
          seq_len=excluded.seq_len,
          model_kind=excluded.model_kind,
          model_blob=excluded.model_blob
        """,
        (
            str(key_type),
            str(key),
            int(horizon_s),
            int(now_ms),
            int(n),
            int(embed_dim),
            int(seq_len),
            str(kind),
            sqlite3.Binary(blob),
        ),
    )


def _set_deterministic(seed: int = _TORCH_SEED) -> None:
    np.random.seed(int(seed))
    torch.manual_seed(int(seed))
    try:
        torch.use_deterministic_algorithms(True)
    except Exception:
        pass
    try:
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
    except Exception:
        pass


# ------            -- ------------------------------------------------------
# Main
# ------            -- ------------------------------------------------------

def main() -> int:
    init_db()

    if not training_allowed():
        print("training disabled by training_guard")
        return 0

    if not acquire_job_lock(JOB_NAME, OWNER, PID):
        print("another training job is running; exiting")
        return 0

    try:
        con0 = connect()
        try:
            con0.executescript(_SCHEMA)
            con0.commit()
        finally:
            con0.close()

        seq_len = int(os.environ.get("TEMPORAL_SEQ_LEN", "6"))
        min_samples = int(os.environ.get("TEMPORAL_MIN_SAMPLES", "120"))
        lookback_days = int(os.environ.get("TEMPORAL_LOOKBACK_DAYS", "365"))
        train_by_class = os.environ.get("TEMPORAL_TRAIN_BY_CLASS", "1") == "1"

        hidden = [int(x) for x in os.environ.get("TEMPORAL_HIDDEN", "256,128").split(",") if x.strip()]
        if not hidden:
            hidden = [256, 128]

        lr = float(os.environ.get("TEMPORAL_LR", "0.003"))
        epochs = int(os.environ.get("TEMPORAL_EPOCHS", "120"))
        weight_decay = float(os.environ.get("TEMPORAL_WEIGHT_DECAY", "0.0001"))

        # Optional: cap CPU threads for stability in 24/7 envs (leave default if unset)
        try:
            _threads = int(os.environ.get("TORCH_NUM_THREADS", "0"))
            if _threads > 0:
                torch.set_num_threads(_threads)
        except Exception:
            pass

        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - int(lookback_days) * 86400 * 1000

        # ----------------------------
        # Phase 1: READ + BUILD DATA (no torch training yet)
        # ----------------------------
        con_r = connect()
        try:
            rows = con_r.execute(
                """
                SELECT l.event_id, l.symbol, l.horizon_s, l.impact_z, e.ts_ms
                FROM labels l
                JOIN events e ON e.id = l.event_id
                WHERE e.ts_ms >= ?
                  AND l.impact_z IS NOT NULL
                """,
                (int(cutoff_ms),),
            ).fetchall()

            buckets: Dict[Tuple[str, str, int], List[Tuple[np.ndarray, float, int]]] = {}

            for eid, sym, h, z, ts_ms in rows or []:
                sym_u = str(sym or "").upper().strip()
                if not sym_u:
                    continue
                h_i = int(h or 0)
                if h_i <= 0:
                    continue

                built = _build_sequence_flat(con_r, int(ts_ms), int(seq_len))
                if not built:
                    continue
                x, embed_dim = built

                try:
                    y = float(z)
                    if not np.isfinite(y):
                        continue
                except Exception:
                    continue

                buckets.setdefault(("symbol", sym_u, h_i), []).append((x, y, embed_dim))

                if train_by_class:
                    cls = asset_class_for_symbol(sym_u)
                    if cls and str(cls).upper() != "UNKNOWN":
                        buckets.setdefault(("class", str(cls).upper(), h_i), []).append((x, y, embed_dim))

            # Add global bucket per horizon (from symbol buckets only)
            for (kt, key, h_i), items in list(buckets.items()):
                if kt != "symbol":
                    continue
                buckets.setdefault(("global", "ALL", h_i), []).extend(items)

        finally:
            con_r.close()

        # ----------------------------
        # Phase 2: TRAIN (no DB open)
        # ----------------------------
        trained = 0
        to_write: List[Tuple[str, str, int, int, int, int, int, str, bytes, int, int, float, float, float]] = []
        # tuple fields:
        # (kt, key, h_i, now_ms, n, embed_dim, seq_len, kind, blob, n_train, n_eval, rmse, spearman, directional_acc)

        for (kt, key, h_i), items in buckets.items():
            if len(items) < int(min_samples):
                continue

            embed_dim = int(items[0][2])
            xs: List[np.ndarray] = []
            ys: List[float] = []
            ok = True
            for x, yv, ed in items:
                if int(ed) != int(embed_dim):
                    ok = False
                    break
                if not np.isfinite(float(yv)):
                    ok = False
                    break
                xs.append(x)
                ys.append(float(yv))
            if not ok:
                continue

            X = np.stack(xs).astype(np.float32, copy=False)
            y = np.asarray(ys, dtype=np.float32)

            n = int(len(y))
            split = max(1, int(n * 0.8))
            Xtr, Xev = X[:split], X[split:]
            ytr, yev = y[:split], y[split:]
            if Xev.shape[0] == 0 or Xtr.shape[0] == 0:
                continue

            _set_deterministic()

            x_mean = Xtr.mean(axis=0).astype(np.float32)
            x_std = Xtr.std(axis=0).astype(np.float32)
            x_std = np.where(x_std < 1e-6, 1.0, x_std)

            y_mean = float(ytr.mean())
            y_std_val = float(ytr.std())
            y_std = float(y_std_val if np.isfinite(y_std_val) and y_std_val > 1e-6 else 1.0)

            Xtrn = (Xtr - x_mean) / x_std
            ytrn = (ytr - y_mean) / y_std
            Xevn = (Xev - x_mean) / x_std

            xt = torch.from_numpy(Xtrn)
            yt = torch.from_numpy(ytrn)

            model = _TemporalMLP(input_dim=int(Xtr.shape[1]), hidden=hidden)
            opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)
            loss_fn = nn.MSELoss()

            model.train()
            for _ in range(int(epochs)):
                opt.zero_grad(set_to_none=True)
                pred_t = model(xt)
                loss = loss_fn(pred_t, yt)
                loss.backward()
                opt.step()

            model.eval()
            with torch.no_grad():
                pred_n = model(torch.from_numpy(Xevn)).cpu().numpy().astype(np.float32, copy=False)
            pred = pred_n * y_std + y_mean

            rmse, sp, da = _eval_predictions(yev, pred)

            payload = {
                "kind": "temporal_mlp",
                "input_dim": int(Xtr.shape[1]),
                "hidden": hidden,
                "state_dict": model.state_dict(),
                "x_mean": x_mean,
                "x_std": x_std,
                "y_mean": float(y_mean),
                "y_std": float(y_std),
                "seq_len": int(seq_len),
                "embed_dim": int(embed_dim),
            }

            blob = _serialize_payload(payload)

            to_write.append(
                (
                    kt,
                    key,
                    int(h_i),
                    int(now_ms),
                    int(n),
                    int(embed_dim),
                    int(seq_len),
                    "temporal_mlp",
                    blob,
                    int(len(ytr)),
                    int(len(yev)),
                    float(rmse),
                    float(sp),
                    float(da),
                )
            )

        # ----------------------------
        # Phase 3: WRITE (short transaction)
        # ----------------------------
        con_w = connect()
        try:
            for (
                kt,
                key,
                h_i,
                now_ms2,
                n2,
                embed_dim2,
                seq_len2,
                kind,
                blob,
                n_train,
                n_eval,
                rmse,
                sp,
                da,
            ) in to_write:
                _upsert_model(
                    con_w,
                    key_type=kt,
                    key=key,
                    horizon_s=h_i,
                    now_ms=now_ms2,
                    n=n2,
                    embed_dim=embed_dim2,
                    seq_len=seq_len2,
                    kind=kind,
                    blob=blob,
                )

                con_w.execute(
                    """
                    INSERT OR REPLACE INTO temporal_model_eval(
                      key_type, key, horizon_s, model_kind, ts_ms,
                      n_train, n_eval, rmse, spearman, directional_acc
                    )
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        kt,
                        key,
                        h_i,
                        kind,
                        now_ms2,
                        n_train,
                        n_eval,
                        rmse,
                        sp,
                        da,
                    ),
                )

                trained += 1

            con_w.commit()
        finally:
            con_w.close()

        print(json.dumps({"ok": True, "trained": trained}, indent=2))
        return 0

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
