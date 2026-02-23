# eval_temporal_shadow.py
"""
A.7 Shadow Evaluation for Temporal Predictor.

Builds temporal_shadow_eval from:
  - temporal_predictions (shadow logs)
  - labels (realized)
  - predictions (baseline)

Usage:
  python eval_temporal_shadow.py
"""

import json
import os
import time
import math
from typing import Dict, Any, Tuple

import numpy as np

from engine.storage import connect, init_db


SCHEMA = """
CREATE TABLE IF NOT EXISTS temporal_shadow_eval (
  symbol TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL,

  n INTEGER NOT NULL,

  rmse REAL NOT NULL,
  baseline_rmse REAL NOT NULL,

  directional_acc REAL NOT NULL,
  baseline_directional_acc REAL NOT NULL,

  pass_all INTEGER NOT NULL,
  detail_json TEXT NOT NULL,

  PRIMARY KEY (symbol, horizon_s)
);

CREATE INDEX IF NOT EXISTS idx_temporal_shadow_eval_ts
  ON temporal_shadow_eval(ts_ms);
"""


def _rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return 0.0
    e = (y_true - y_pred).astype(float)
    return float(math.sqrt(float(np.mean(e * e))))


def _diracc(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return 0.0
    eps = 1e-9
    yt = np.sign(np.where(np.abs(y_true) < eps, 0.0, y_true))
    yp = np.sign(np.where(np.abs(y_pred) < eps, 0.0, y_pred))
    return float(np.mean(yt == yp))


def main() -> int:
    init_db()
    init_validation_db()
    con = connect()
    try:
        con.executescript(SCHEMA)
        con.commit()

        now_ms = int(time.time() * 1000)
        lookback_days = int(os.environ.get("TEMPORAL_EVAL_LOOKBACK_DAYS", "90"))
        min_ts = now_ms - int(lookback_days) * 86400 * 1000

        min_n = int(os.environ.get("TEMPORAL_PROMOTE_MIN_N", "200"))
        min_improve = float(os.environ.get("TEMPORAL_PROMOTE_MIN_IMPROVEMENT", os.environ.get("PROMOTE_MIN_IMPROVEMENT", "0.01")))
        dir_tol = float(os.environ.get("TEMPORAL_PROMOTE_DIRACC_TOL", os.environ.get("PROMOTE_DIRACC_TOL", "0.00")))

        # Align on labeled rows where we have temporal predictions
        rows = con.execute(
            """
            SELECT
              tp.symbol,
              tp.horizon_s,
              tp.event_id,
              tp.pred_z AS temporal_pred,
              l.impact_z AS y_true,
              p.predicted_z AS baseline_pred,
              tp.model_ts_ms AS model_ts_ms
            FROM temporal_predictions tp
            JOIN labels l
              ON l.event_id = tp.event_id
             AND l.symbol = tp.symbol
             AND l.horizon_s = tp.horizon_s
            LEFT JOIN predictions p
              ON p.event_id = tp.event_id
             AND p.symbol = tp.symbol
             AND p.horizon_s = tp.horizon_s
            WHERE tp.ts_ms >= ?
              AND l.impact_z IS NOT NULL
            """,
            (int(min_ts),),
        ).fetchall()

        by: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for sym, h, eid, tpred, y, bpred, model_ts_ms in rows or []:
            sym_u = str(sym or "").upper().strip()
            hi = int(h or 0)
            if not sym_u or hi <= 0:
                continue

            try:
                yt = float(y)
                tpv = float(tpred)
            except Exception:
                continue
            if not np.isfinite(yt) or not np.isfinite(tpv):
                continue

            # baseline can be missing; skip those rows from baseline metric
            b_ok = False
            try:
                bpv = float(bpred)
                if np.isfinite(bpv):
                    b_ok = True
            except Exception:
                bpv = 0.0

            k = (sym_u, hi)
            g = by.get(k)
            if g is None:
                g = {
                    "y": [],
                    "t": [],
                    "b_y": [],
                    "b": [],
                    "latest_model_ts_ms": int(model_ts_ms or 0),
                }
                by[k] = g

            g["y"].append(yt)
            g["t"].append(tpv)

            if b_ok:
                g["b_y"].append(yt)
                g["b"].append(bpv)

            # track latest model_ts_ms we observed
            try:
                mts = int(model_ts_ms or 0)
                if mts > int(g["latest_model_ts_ms"] or 0):
                    g["latest_model_ts_ms"] = mts
            except Exception:
                pass

        cur = con.cursor()

        for (sym, hi), g in by.items():
            y = np.asarray(g["y"], dtype=np.float32)
            t = np.asarray(g["t"], dtype=np.float32)

            b_y = np.asarray(g["b_y"], dtype=np.float32)
            b = np.asarray(g["b"], dtype=np.float32)

            n = int(y.size)
            if n <= 0:
                continue

            rmse = _rmse(y, t)
            da = _diracc(y, t)

            # baseline: if we have none, treat as "infinite worse"
            if b_y.size >= 10:
                brmse = _rmse(b_y, b)
                bda = _diracc(b_y, b)
            else:
                brmse = float("inf")
                bda = 0.0

            # gates
            reasons = []
            pass_all = True

            if n < min_n:
                pass_all = False
                reasons.append(f"n<{min_n}")

            if not np.isfinite(brmse) or brmse <= 0:
                # baseline unavailable: do not allow auto-promote
                pass_all = False
                reasons.append("baseline_unavailable")
            else:
                # must improve rmse
                if not (rmse < brmse * (1.0 - min_improve)):
                    pass_all = False
                    reasons.append("rmse_not_improved")

                # must not regress directional accuracy beyond tolerance
                if not (da >= (bda - dir_tol)):
                    pass_all = False
                    reasons.append("diracc_regressed")

            detail = {
                "symbol": sym,
                "horizon_s": hi,
                "n": n,
                "rmse": float(rmse),
                "baseline_rmse": float(brmse) if np.isfinite(brmse) else None,
                "directional_acc": float(da),
                "baseline_directional_acc": float(bda),
                "min_n": int(min_n),
                "min_improve": float(min_improve),
                "diracc_tol": float(dir_tol),
                "reasons": reasons,
                "latest_model_ts_ms": int(g.get("latest_model_ts_ms") or 0),
            }

            cur.execute(
                """
                INSERT INTO temporal_shadow_eval(
                  symbol, horizon_s, ts_ms,
                  n, rmse, baseline_rmse,
                  directional_acc, baseline_directional_acc,
                  pass_all, detail_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol, horizon_s) DO UPDATE SET
                  ts_ms=excluded.ts_ms,
                  n=excluded.n,
                  rmse=excluded.rmse,
                  baseline_rmse=excluded.baseline_rmse,
                  directional_acc=excluded.directional_acc,
                  baseline_directional_acc=excluded.baseline_directional_acc,
                  pass_all=excluded.pass_all,
                  detail_json=excluded.detail_json
                """,
                (
                    sym,
                    int(hi),
                    int(now_ms),
                    int(n),
                    float(rmse),
                    float(brmse) if np.isfinite(brmse) else float("inf"),
                    float(da),
                    float(bda),
                    1 if pass_all else 0,
                    json.dumps(detail, separators=(",", ":"), sort_keys=True),
                ),
            )

        con.commit()
        print(json.dumps({"ok": True, "groups": len(by)}, indent=2))
        return 0

    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
