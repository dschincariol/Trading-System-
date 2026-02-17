# dev_core/embed_regressor.py
"""
Supervised embedding->impact_z regressor (Option A + A.4).

Stores models in SQLite (embed_models2):
- key_type='symbol'  key='<SYMBOL>'
- key_type='class'   key='<ASSET_CLASS>'

We support multiple model kinds while keeping the SAME table schema:
- kind='ridge' (legacy / default)
- kind='mlp'   (torch MLP)

Storage format (model_blob):
- Legacy ridge blobs (backward compatible):
    [int32 dim][float32 coef...][float32 intercept]
- MLP blobs:
    b"MLP1" + torch.save(payload_dict)
    payload_dict includes:
      - state_dict
      - input_dim, hidden, dropout
      - x_mean, x_std
      - y_mean, y_std

Inference order:
1) symbol model
2) asset-class model
3) None (caller can fallback to KNN)
"""

import io
import os
import time
import sqlite3
from typing import Dict, Tuple, Optional, List

import math
import json
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.isotonic import IsotonicRegression

import torch
import torch.nn as nn

from engine.dev_core.storage import connect
from engine.dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from engine.dev_core.feature_expansion import build_feature_vector, feature_set_tag
from ops.asset_map import asset_class_for_symbol
_SCHEMA = """
CREATE TABLE IF NOT EXISTS embed_models2 (
  key_type TEXT NOT NULL,             -- 'symbol' | 'class'
  key TEXT NOT NULL,                  -- e.g. 'SPY' or 'EQUITY'
  horizon_s INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL,
  n INTEGER NOT NULL,
  dim INTEGER NOT NULL,
  model_blob BLOB NOT NULL,
  PRIMARY KEY(key_type, key, horizon_s)
);

CREATE INDEX IF NOT EXISTS idx_embed_models2_ts ON embed_models2(ts_ms);
CREATE INDEX IF NOT EXISTS idx_embed_models2_lookup ON embed_models2(key_type, key, horizon_s);

-- A1: offline evaluation metrics for trained embed models
CREATE TABLE IF NOT EXISTS embed_model_eval (
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

-- A2: confidence calibration curve (isotonic) per horizon + model_kind
CREATE TABLE IF NOT EXISTS embed_conf_calib (
  horizon_s INTEGER NOT NULL,
  model_kind TEXT NOT NULL,
  ts_ms INTEGER NOT NULL,
  conf_k REAL NOT NULL,
  n_points INTEGER NOT NULL,
  x_json TEXT NOT NULL,
  y_json TEXT NOT NULL,
  PRIMARY KEY (horizon_s, model_kind)
);

-- Weather contribution tracking (base vs weather)
CREATE TABLE IF NOT EXISTS model_weather_effect (
  key_type TEXT NOT NULL,        -- 'symbol' | 'class'
  key TEXT NOT NULL,             -- raw key (not namespaced)
  horizon_s INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL,

  base_rmse REAL,
  wx_rmse REAL,
  rmse_delta REAL,

  base_spearman REAL,
  wx_spearman REAL,
  spearman_delta REAL,

  n_eval INTEGER NOT NULL,
  PRIMARY KEY (key_type, key, horizon_s, ts_ms)
);

"""

# --- MLP blob magic header (new) ---
_MLP_MAGIC = b"MLP1"

# --- Determinism defaults ---
_TORCH_SEED = 42


def init_embed_models_db() -> None:
    con = connect()
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()


# =========================
# Ridge (legacy) serialization
# =========================

def _serialize_ridge(model: Ridge) -> bytes:
    coef = np.asarray(model.coef_, dtype=np.float32).reshape(-1)
    intercept = np.asarray([float(model.intercept_)], dtype=np.float32)
    buf = io.BytesIO()
    buf.write(np.int32(coef.shape[0]).tobytes())
    buf.write(coef.tobytes())
    buf.write(intercept.tobytes())
    return buf.getvalue()


def _deserialize_ridge(blob: bytes) -> Tuple[np.ndarray, float]:
    b = memoryview(blob)
    dim = int(np.frombuffer(b[:4], dtype=np.int32)[0])
    off = 4
    coef = np.frombuffer(b[off:off + (dim * 4)], dtype=np.float32).copy()
    off += dim * 4
    intercept = float(np.frombuffer(b[off:off + 4], dtype=np.float32)[0])
    return coef, intercept


# =========================
# MLP model + serialization
# =========================

class _MLPRegressor(nn.Module):
    def __init__(self, input_dim: int, hidden: List[int], dropout: float = 0.0):
        super().__init__()
        dims = [int(input_dim)] + [int(h) for h in (hidden or [])] + [1]
        layers: List[nn.Module] = []
        for i in range(len(dims) - 2):
            layers.append(nn.Linear(dims[i], dims[i + 1]))
            layers.append(nn.ReLU())
            if float(dropout) > 0.0:
                layers.append(nn.Dropout(float(dropout)))
        layers.append(nn.Linear(dims[-2], dims[-1]))
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x).squeeze(-1)


# =========================
# Temporal MLP Encoder (Step B) — (kept; not used here yet)
# =========================

class _TemporalMLP(nn.Module):
    def __init__(self, input_dim: int, hidden: List[int]):
        super().__init__()
        dims = [int(input_dim)] + [int(h) for h in (hidden or [])] + [int(input_dim)]
        layers: List[nn.Module] = []
        for i in range(len(dims) - 2):
            layers.append(nn.Linear(dims[i], dims[i + 1]))
            layers.append(nn.ReLU())
        layers.append(nn.Linear(dims[-2], dims[-1]))
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


def _serialize_mlp_payload(payload: Dict) -> bytes:
    buf = io.BytesIO()
    torch.save(payload, buf)
    return _MLP_MAGIC + buf.getvalue()


def _deserialize_mlp_payload(blob: bytes) -> Dict:
    if not blob.startswith(_MLP_MAGIC):
        raise ValueError("not an MLP blob")
    raw = blob[len(_MLP_MAGIC):]
    buf = io.BytesIO(raw)
    payload = torch.load(buf, map_location="cpu")
    if not isinstance(payload, dict):
        raise ValueError("invalid MLP payload")
    return payload


def _upsert_model(con, key_type: str, key: str, horizon_s: int, now_ms: int, n: int, dim: int, blob_out: bytes) -> None:
    con.execute(
        """
        INSERT INTO embed_models2(key_type, key, horizon_s, ts_ms, n, dim, model_blob)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key_type, key, horizon_s) DO UPDATE SET
          ts_ms=excluded.ts_ms,
          n=excluded.n,
          dim=excluded.dim,
          model_blob=excluded.model_blob
        """,
        (str(key_type), str(key), int(horizon_s), int(now_ms), int(n), int(dim), sqlite3.Binary(blob_out)),
    )


def _eval_predictions(y_true: np.ndarray, y_pred: np.ndarray) -> Tuple[float, float, float]:
    """
    Returns: (rmse, spearman_ic, directional_accuracy)
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)

    if y_true.size == 0:
        return 0.0, 0.0, 0.0

    rmse = float(np.sqrt(np.mean((y_true - y_pred) ** 2)))

    # Spearman via rank-corr
    try:
        rt = y_true.argsort().argsort()
        rp = y_pred.argsort().argsort()
        spearman = float(np.corrcoef(rt, rp)[0, 1])
        if not np.isfinite(spearman):
            spearman = 0.0
    except Exception:
        spearman = 0.0

    try:
        directional = float(np.mean(np.sign(y_true) == np.sign(y_pred)))
    except Exception:
        directional = 0.0

    return rmse, spearman, directional


def _conf_from_n(n_train: int, conf_k: float) -> float:
    # conf_raw = 1 - exp(-n/k)
    try:
        n = max(0, int(n_train))
        k = float(conf_k)
        if k <= 1e-9:
            return 0.0
        c = 1.0 - math.exp(-float(n) / float(k))
        return float(max(0.0, min(1.0, c)))
    except Exception:
        return 0.0


def _fit_isotonic_curve(xs, ys) -> Optional[Tuple[List[float], List[float]]]:
    """
    Fit isotonic regression y=f(x) with clipping, return sorted unique curve points.
    xs, ys are lists of floats.
    """
    xs = [float(x) for x in (xs or []) if x is not None and np.isfinite(float(x))]
    ys = [float(y) for y in (ys or []) if y is not None and np.isfinite(float(y))]
    if len(xs) < 5 or len(xs) != len(ys):
        return None

    # sort by x
    pairs = sorted(zip(xs, ys), key=lambda t: t[0])
    x = np.asarray([p[0] for p in pairs], dtype=float)
    y = np.asarray([p[1] for p in pairs], dtype=float)

    try:
        iso = IsotonicRegression(out_of_bounds="clip")
        yhat = iso.fit_transform(x, y)
    except Exception:
        return None

    # compress to unique x points (keeps last y per x)
    out_x: List[float] = []
    out_y: List[float] = []
    last_x: Optional[float] = None
    for xi, yi in zip(x.tolist(), yhat.tolist()):
        if last_x is None or float(xi) != float(last_x):
            out_x.append(float(xi))
            out_y.append(float(yi))
            last_x = float(xi)
        else:
            out_y[-1] = float(yi)

    if len(out_x) < 2:
        return None

    return out_x, out_y

def train_embed_models(
    symbols: List[str],
    horizons: List[int],
    min_samples: int = 50,
    alpha: float = 1.0,
    lookback_days: int = 365,
    train_by_class: bool = True,
    kind: str = "ridge",  # 'ridge' | 'mlp' | 'auto'
    mlp_hidden: Optional[List[int]] = None,
    mlp_dropout: float = 0.0,
    mlp_lr: float = 3e-3,
    mlp_epochs: int = 120,
    mlp_weight_decay: float = 1e-4,
) -> Dict[Tuple[str, str, int], int]:
    """
    Trains models for:
      (key_type='symbol', key=symbol, horizon_s)
    and optionally:
      (key_type='class', key=asset_class, horizon_s)

    Returns dict[(key_type,key,horizon_s)] = n_trained
    """
    init_embed_models_db()

    kind = str(kind or "ridge").strip().lower()
    if kind not in ("ridge", "mlp", "auto"):
        kind = "ridge"

    # must match predictor's EMBED_REGRESSOR_CONF_K default
    conf_k = float(os.environ.get("EMBED_REGRESSOR_CONF_K", "75.0"))

    if mlp_hidden is None:
        mlp_hidden = [128, 64]

    now_ms = int(time.time() * 1000)

    cutoff_ms = now_ms - int(lookback_days) * 24 * 3600 * 1000

    tag = feature_set_tag()
    def _tag_key(k: str) -> str:
        # Keep backward-compatible keys for existing deployments.
        # Only namespace when tag != "base".
        return str(k) if tag == "base" else f"{str(k)}#{tag}"
    symset = set(str(s).upper() for s in (symbols or []))
    hset = set(int(h) for h in (horizons or []))

    con = connect()
    try:
        # A2: calibration samples accumulator: (horizon_s, model_kind) -> {"x":[], "y":[]}
        _calib: Dict[Tuple[int, str], Dict[str, List[float]]] = {}

        rows = con.execute(
            """
            SELECT
              l.event_id,
              l.symbol,
              l.horizon_s,
              COALESCE(le.net_z, l.impact_z) AS impact_z,
              emb.vec,
              e.ts_ms
            FROM labels l
            JOIN events e ON e.id = l.event_id
            JOIN event_embeddings emb ON emb.event_id = l.event_id
            LEFT JOIN labels_exec le
              ON le.event_id = l.event_id
             AND le.symbol   = l.symbol
             AND le.horizon_s = l.horizon_s
             AND le.realized = 1
            WHERE e.ts_ms >= ?
              AND COALESCE(le.net_z, l.impact_z) IS NOT NULL
            """,
            (int(cutoff_ms),),
        ).fetchall()

        # buckets for symbol: (sym,h) -> list[(impact_z, emb_blob, event_ts_ms, sym_u)]
        sym_buckets: Dict[Tuple[str, int], List[Tuple[float, bytes, int, str]]] = {}

        # buckets for class: (cls,h) -> list[(impact_z, emb_blob, event_ts_ms, sym_u)]
        cls_buckets: Dict[Tuple[str, int], List[Tuple[float, bytes, int, str]]] = {}

        for _eid, sym, h, z, blob, ts_ms in rows or []:
            sym_u = str(sym).upper()
            h_i = int(h)
            if symset and sym_u not in symset:
                continue
            if hset and h_i not in hset:
                continue
            if blob is None:
                continue
            try:
                zz = float(z)
            except Exception:
                continue

            ts_i = int(ts_ms or 0)

            sym_buckets.setdefault((sym_u, h_i), []).append((zz, blob, ts_i, sym_u))

            if train_by_class:
                cls = asset_class_for_symbol(sym_u)
                cls_buckets.setdefault((str(cls).upper(), h_i), []).append((zz, blob, ts_i, sym_u))

        out: Dict[Tuple[str, str, int], int] = {}

        def _build_xy(items: List[Tuple[float, bytes, int, str]]) -> Optional[Tuple[np.ndarray, np.ndarray]]:
            if len(items) < int(min_samples):
                return None

            vecs: List[np.ndarray] = []
            ys: List[float] = []
            for zz, b, ts_i, sym_u in items:
                v0 = np.frombuffer(b, dtype=np.float32)
                feats = build_feature_vector(
                    event={"ts_ms": int(ts_i), "title": "", "body": "", "source": ""},
                    symbol=str(sym_u),
                )
                v = np.concatenate([v0, np.asarray(feats, dtype=np.float32)])
                vecs.append(v)
                ys.append(float(zz))

            X = np.stack(vecs).astype(np.float32, copy=False)
            y = np.asarray(ys, dtype=np.float32)
            return X, y

        def _train_mlp(X: np.ndarray, y: np.ndarray) -> bytes:
            # deterministic
            np.random.seed(_TORCH_SEED)
            torch.manual_seed(_TORCH_SEED)

            Xf = X.astype(np.float32, copy=False)
            yf = y.astype(np.float32, copy=False)

            x_mean = Xf.mean(axis=0).astype(np.float32)
            x_std = Xf.std(axis=0).astype(np.float32)
            x_std = np.where(x_std < 1e-6, 1.0, x_std).astype(np.float32)

            y_mean = np.float32(yf.mean())
            y_std_val = float(yf.std())
            y_std = np.float32(y_std_val if y_std_val > 1e-6 else 1.0)

            Xn = (Xf - x_mean) / x_std
            yn = (yf - y_mean) / y_std

            xt = torch.from_numpy(Xn)
            yt = torch.from_numpy(yn)

            model = _MLPRegressor(input_dim=int(Xn.shape[1]), hidden=list(mlp_hidden or []), dropout=float(mlp_dropout))
            opt = torch.optim.AdamW(model.parameters(), lr=float(mlp_lr), weight_decay=float(mlp_weight_decay))
            loss_fn = nn.MSELoss()

            model.train()
            for _epoch in range(int(mlp_epochs)):
                opt.zero_grad(set_to_none=True)
                pred = model(xt)
                loss = loss_fn(pred, yt)
                loss.backward()
                opt.step()

            payload = {
                "kind": "mlp1",
                "input_dim": int(Xn.shape[1]),
                "hidden": [int(h) for h in (mlp_hidden or [])],
                "dropout": float(mlp_dropout),
                "state_dict": model.state_dict(),
                "x_mean": x_mean,
                "x_std": x_std,
                "y_mean": y_mean,
                "y_std": y_std,
            }
            return _serialize_mlp_payload(payload)

        def _train_one(items: List[Tuple[float, bytes, int, str]]):
            built = _build_xy(items)
            if not built:
                return None
            X, y = built
            dim = int(X.shape[1])

            n = int(len(y))
            split = max(1, int(n * 0.8))
            Xtr, Xev = X[:split], X[split:]
            ytr, yev = y[:split], y[split:]

            results: Dict[str, Tuple[bytes, Dict[str, float]]] = {}

            # --------------------------------------------------
            # Weather contribution test (ridge-only, same split)
            # --------------------------------------------------
            try:
                Xb, yb = _build_xy(items)
                Xw, yw = _build_xy(items)

                if Xb is not None and Xw is not None:
                    mr = Ridge(alpha=float(alpha), fit_intercept=True)
                    mr.fit(Xb[:split], yb[:split])
                    pb = mr.predict(Xb[split:])
                    brmse, bsp, _ = _eval_predictions(yb[split:], pb)

                    mw = Ridge(alpha=float(alpha), fit_intercept=True)
                    mw.fit(Xw[:split], yw[:split])
                    pw = mw.predict(Xw[split:])
                    wrmse, wsp, _ = _eval_predictions(yw[split:], pw)

                    con.execute(
                        """
                        INSERT OR REPLACE INTO model_weather_effect(
                          key_type, key, horizon_s, ts_ms,
                          base_rmse, wx_rmse, rmse_delta,
                          base_spearman, wx_spearman, spearman_delta,
                          n_eval
                        )
                        VALUES ('__PENDING__','__PENDING__',-1,?,?,?,?,?,?,?,?)
                        """,
                        (
                            int(now_ms),
                            float(brmse),
                            float(wrmse),
                            float(brmse) - float(wrmse),
                            float(bsp),
                            float(wsp),
                            float(wsp) - float(bsp),
                            int(len(yb[split:])),
                        ),
                    )
            except Exception:
                pass

            # --- Ridge ---

            try:
                model_r = Ridge(alpha=float(alpha), fit_intercept=True)
                model_r.fit(Xtr, ytr)
                pred_r = model_r.predict(Xev)
                blob_r = _serialize_ridge(model_r)
                rmse, sp, da = _eval_predictions(yev, pred_r)
                results["ridge"] = (blob_r, {
                    "n_train": int(len(ytr)),
                    "n_eval": int(len(yev)),
                    "rmse": float(rmse),
                    "spearman": float(sp),
                    "directional_acc": float(da),
                })
            except Exception:
                pass

            # --- MLP ---
            try:
                blob_m = _train_mlp(Xtr, ytr)
                payload = _deserialize_mlp_payload(blob_m)
                model = _MLPRegressor(
                    input_dim=int(payload["input_dim"]),
                    hidden=payload["hidden"],
                    dropout=float(payload["dropout"]),
                )
                model.load_state_dict(payload["state_dict"])
                model.eval()

                xn = (Xev - payload["x_mean"]) / payload["x_std"]
                with torch.no_grad():
                    pred_n = model(torch.from_numpy(xn.astype(np.float32, copy=False))).numpy()
                pred_m = pred_n * float(payload["y_std"]) + float(payload["y_mean"])
                rmse, sp, da = _eval_predictions(yev, pred_m)
                results["mlp"] = (blob_m, {
                    "n_train": int(len(ytr)),
                    "n_eval": int(len(yev)),
                    "rmse": float(rmse),
                    "spearman": float(sp),
                    "directional_acc": float(da),
                })
            except Exception:
                pass

            if not results:
                return None

            # choose best (A3) if auto; otherwise force requested kind
            if kind == "auto":
                best_kind = None
                best_em = None
                for k, (_blob, em) in results.items():
                    if best_em is None:
                        best_kind = k
                        best_em = em
                        continue
                    if float(em["rmse"]) < float(best_em["rmse"]) - 1e-12:
                        best_kind = k
                        best_em = em
                    elif abs(float(em["rmse"]) - float(best_em["rmse"])) <= 1e-12 and float(em["spearman"]) > float(best_em["spearman"]):
                        best_kind = k
                        best_em = em
                chosen_kind = best_kind
            else:
                chosen_kind = kind if kind in results else ("ridge" if "ridge" in results else list(results.keys())[0])

            chosen_blob, _ = results[str(chosen_kind)]
            return dim, str(chosen_kind), chosen_blob, results

        # train symbol models
        for (sym_u, h_i), items in sym_buckets.items():
            res = _train_one(items)
            if not res:
                continue
            dim, _chosen_kind, blob_out, results = res

            # A1: store eval rows for all trained kinds
            for mk, (_b, em) in (results or {}).items():
                con.execute(
                    """
                    INSERT OR REPLACE INTO embed_model_eval(
                      key_type, key, horizon_s, model_kind, ts_ms,
                      n_train, n_eval, rmse, spearman, directional_acc
                    )
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "symbol",
                        sym_u,
                        int(h_i),
                        str(mk),
                        int(now_ms),
                        int(em["n_train"]),
                        int(em["n_eval"]),
                        float(em["rmse"]),
                        float(em["spearman"]),
                        float(em["directional_acc"]),
                    ),
                )

                # A2 calibration samples
                try:
                    _calib.setdefault((int(h_i), str(mk)), {"x": [], "y": []})
                    _calib[(int(h_i), str(mk))]["x"].append(
                        _conf_from_n(int(em["n_train"]), float(conf_k))
                    )
                    _calib[(int(h_i), str(mk))]["y"].append(
                        float(em["directional_acc"])
                    )
                except Exception:
                    pass

            # A3: store only winner blob
            _upsert_model(con, "symbol", _tag_key(sym_u), h_i, now_ms, len(items), int(dim), blob_out)
            out[("symbol", _tag_key(sym_u), h_i)] = int(len(items))

            # Fill pending weather-effect row (if any) for this (symbol,h)
            try:
                con.execute(
                    """
                    UPDATE model_weather_effect
                    SET key_type='symbol', key=?, horizon_s=?
                    WHERE key_type='__PENDING__' AND key='__PENDING__' AND horizon_s=-1 AND ts_ms=?
                    """,
                    (str(sym_u), int(h_i), int(now_ms)),
                )
            except Exception:
                pass

        # train class models
        if train_by_class:
            for (cls, h_i), items in cls_buckets.items():
                res = _train_one(items)
                if not res:
                    continue
                dim, _chosen_kind, blob_out, results = res

                for mk, (_b, em) in (results or {}).items():

                    try:
                        _calib.setdefault((int(h_i), str(mk)), {"x": [], "y": []})
                        _calib[(int(h_i), str(mk))]["x"].append(
                            _conf_from_n(int(em["n_train"]), float(conf_k))
                        )
                        _calib[(int(h_i), str(mk))]["y"].append(
                            float(em["directional_acc"])
                        )
                    except Exception:
                        pass

                _upsert_model(
                    con,
                    "class",
                    _tag_key(str(cls).upper()),
                    h_i,
                    now_ms,
                    len(items),
                    int(dim),
                    blob_out,
                )
                out[("class", _tag_key(str(cls).upper()), h_i)] = int(len(items))

        # -----------------------------------
        # A2: fit + persist confidence calibration curves
        # -----------------------------------

        con.commit()
        return out

    finally:
        con.close()


def _predict_raw(
    key_type: str,
    key: str,
    horizon_s: int,
    query_vec: np.ndarray
) -> Optional[Tuple[float, int, int, str, str, str]]:
    """
    Returns:
      (predicted_z, n_support, model_ts_ms, model_key_type, model_key, model_kind)
    """
    init_embed_models_db()
    con = connect()
    try:
        row = con.execute(
            """
            SELECT ts_ms, n, dim, model_blob
            FROM embed_models2
            WHERE key_type=? AND key=? AND horizon_s=?
            """,
            (str(key_type), str(key), int(horizon_s)),
        ).fetchone()

        if not row:
            return None

        ts_ms, n, dim, blob = row
        if blob is None:
            return None

        q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        if q.shape[0] != int(dim):
            return None

        # --- MLP path ---
        try:
            if bytes(blob).startswith(_MLP_MAGIC):
                payload = _deserialize_mlp_payload(bytes(blob))
                if int(payload.get("input_dim") or 0) != int(dim):
                    return None

                x_mean = np.asarray(payload.get("x_mean"), dtype=np.float32).reshape(-1)
                x_std = np.asarray(payload.get("x_std"), dtype=np.float32).reshape(-1)
                if x_mean.shape[0] != int(dim) or x_std.shape[0] != int(dim):
                    return None
                x_std = np.where(x_std < 1e-6, 1.0, x_std).astype(np.float32)

                y_mean = float(payload.get("y_mean") or 0.0)
                y_std = float(payload.get("y_std") or 1.0)
                if abs(float(y_std)) < 1e-6:
                    y_std = 1.0

                hidden = payload.get("hidden") or [128, 64]
                dropout = float(payload.get("dropout") or 0.0)

                model = _MLPRegressor(input_dim=int(dim), hidden=[int(h) for h in hidden], dropout=float(dropout))
                sd = payload.get("state_dict")
                if not isinstance(sd, dict):
                    return None
                model.load_state_dict(sd)
                model.eval()

                xn = (q.astype(np.float32) - x_mean) / x_std
                with torch.no_grad():
                    pred_n = float(model(torch.from_numpy(xn.reshape(1, -1))).item())
                pred = float(pred_n * y_std + y_mean)
                return pred, int(n), int(ts_ms), str(key_type), str(key), "mlp"
        except Exception:
            # if MLP blob decode fails, fall through to ridge decode attempt
            pass

        # --- Ridge (legacy) path ---
        coef, intercept = _deserialize_ridge(bytes(blob))
        if coef.shape[0] != int(dim):
            return None
        pred = float(np.dot(coef, q) + float(intercept))
        return pred, int(n), int(ts_ms), str(key_type), str(key), "ridge"

    finally:
        con.close()


def predict_with_embed_model(
    symbol: str,
    horizon_s: int,
    query_vec: np.ndarray
) -> Optional[Tuple[float, int, int, str, str, str]]:
    """
    Returns:
      (predicted_z, n_support, model_ts_ms, model_key_type, model_key, model_kind)
    Tries symbol model first, then asset-class model.

    NOTE:
    - When feature flags change (e.g. weather on/off), we namespace model keys
      with "#<feature_set_tag>" to avoid overwriting existing models.
    - If a namespaced model is missing, we fall back to the legacy key.
    """
    sym_u = str(symbol).upper()
    h = int(horizon_s)

    tag = feature_set_tag()
    sym_key = sym_u if tag == "base" else f"{sym_u}#{tag}"

    r1 = _predict_raw("symbol", sym_key, h, query_vec)
    if r1 is None and sym_key != sym_u:
        r1 = _predict_raw("symbol", sym_u, h, query_vec)
    if r1 is not None:
        return r1

    cls = asset_class_for_symbol(sym_u)
    if cls and str(cls).upper() != "UNKNOWN":
        cls_u = str(cls).upper()
        cls_key = cls_u if tag == "base" else f"{cls_u}#{tag}"

        r2 = _predict_raw("class", cls_key, h, query_vec)
        if r2 is None and cls_key != cls_u:
            r2 = _predict_raw("class", cls_u, h, query_vec)
        if r2 is not None:
            return r2

    return None
