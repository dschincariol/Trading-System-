# dev_core/drift.py
"""
A.2 Model drift detection.

Compares recent absolute error vs historical baseline.
Stores drift metrics per (symbol, horizon_s).

Used ONLY to scale confidence (never prediction).
"""

import time
import numpy as np

from dev_core.storage import connect

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
            return 0

        rows = con.execute(
            """
            SELECT l.symbol, l.horizon_s, l.impact_z
            FROM labels l
            WHERE l.impact_z IS NOT NULL
            ORDER BY l.created_at_ms DESC
            """
        ).fetchall()

        if not rows:
            return {}

        buckets = {}
        for sym, h, z in rows:
            try:
                buckets.setdefault((str(sym), int(h)), []).append(abs(float(z)))
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

            pass

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
