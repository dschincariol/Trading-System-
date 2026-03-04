# dev_core/drift.py
"""
A.2 Model drift detection.

Compares recent absolute error vs historical baseline.
Stores drift metrics per (symbol, horizon_s).

Used ONLY to scale confidence (never prediction).
"""

import os
import time
import numpy as np

from engine.storage import connect
from engine.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot

# optional adaptive retraining
from engine.strategy.training_hooks import schedule_retraining

# thresholds
_RETRAIN_DRIFT_RATIO = float(os.environ.get("DRIFT_RETRAIN_RATIO", "2.0"))
_MAX_LABEL_AGE_MS = int(os.environ.get("DRIFT_MAX_LABEL_AGE_MS", str(24 * 3600 * 1000)))  # 1d

RECENT_N = int(50)        # recent samples
BASELINE_N = int(200)     # long-term baseline
MIN_N = int(30)           # minimum to compute drift


def compute_and_store_drift():
    now_ms = int(time.time() * 1000)

    con = connect()
    try:
        # Fail-soft if labels table isn't created yet
        try:
            con.execute("SELECT 1 FROM labels LIMIT 1").fetchone()
        except Exception:
            return {}

        rows = con.execute(
            """
            SELECT l.symbol, l.horizon_s, l.impact_z, l.created_at_ms
            FROM labels l
            WHERE l.impact_z IS NOT NULL
            ORDER BY l.created_at_ms DESC
            """
        ).fetchall()

        if not rows:
            return {}

        # also track most recent label timestamp per bucket
        buckets = {}
        recent_ts = {}
        for entry in rows:
            try:
                sym, h, z, ts = entry
                sym = str(sym)
                h = int(h)
                zz = abs(float(z))
                key = (sym, h)
                buckets.setdefault(key, []).append(zz)
                # first row is newest because of ORDER BY; we can just record
                if key not in recent_ts or ts is not None and int(ts) > recent_ts.get(key, 0):
                    recent_ts[key] = int(ts or 0)
            except Exception:
                continue

        out = {}

        for (sym, h), errs in buckets.items():
            if len(errs) < MIN_N:
                continue

            recent = np.array(errs[:RECENT_N], dtype=float)
            base = np.array(errs[:BASELINE_N], dtype=float)

            if len(recent) < MIN_N or len(base) < MIN_N:
                continue

            mae_recent = float(np.mean(recent))
            mae_base = float(np.mean(base))

            if mae_base <= 0:
                continue

            drift_ratio = float(mae_recent / mae_base)

            con.execute(
                """
                INSERT INTO model_drift(symbol, horizon_s, ts_ms, n, mae, baseline_mae, drift_ratio)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(symbol, horizon_s) DO UPDATE SET
                  ts_ms=excluded.ts_ms,
                  n=excluded.n,
                  mae=excluded.mae,
                  baseline_mae=excluded.baseline_mae,
                  drift_ratio=excluded.drift_ratio
                """,
                (
                    sym,
                    int(h),
                    now_ms,
                    int(len(recent)),
                    mae_recent,
                    mae_base,
                    drift_ratio,
                ),
            )
            # schedule retraining if drift high or labels stale
            try:
                age_ms = now_ms - int(recent_ts.get((sym, h), 0))
                if drift_ratio >= _RETRAIN_DRIFT_RATIO or age_ms >= _MAX_LABEL_AGE_MS:
                    reason = (
                        "high_drift" if drift_ratio >= _RETRAIN_DRIFT_RATIO else "stale_labels"
                    )
                    schedule_retraining(model_name=os.environ.get("MODEL_NAME", "embed_regressor"), reason=reason)
            except Exception:
                pass

            out[(sym, h)] = {
                "mae": mae_recent,
                "baseline_mae": mae_base,
                "drift_ratio": drift_ratio,
                "n": len(recent),
            }

        con.commit()
        return out

    finally:
        con.close()
