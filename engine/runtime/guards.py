# engine/runtime/guards.py
"""
Runtime Guards:
- Auto champion rollback
- Equity drift classification helpers
"""

import os
import time

from engine.dev_core.storage import connect as _db_connect
from engine.dev_core.model_registry import get_stage_latest


MODEL_NAME = "embed_regressor"


# ---------------------------------------------------
# AUTO ROLLBACK LOOP
# ---------------------------------------------------

def auto_rollback_loop(rollback_fn, write_job_history_fn):
    """
    rollback_fn: callable that executes rollback and returns dict
    write_job_history_fn: persistence hook
    """
    bad_streak = 0

    while True:
        try:
            time.sleep(float(os.environ.get("AUTO_ROLLBACK_POLL_S", "30")))

            champ = get_stage_latest(MODEL_NAME, stage="champion")
            if not champ:
                bad_streak = 0
                continue

            champ_rmse = champ.get("rmse")
            if champ_rmse is None:
                bad_streak = 0
                continue

            window = int(os.environ.get("AUTO_ROLLBACK_WINDOW", "100"))
            sustained = int(os.environ.get("AUTO_ROLLBACK_SUSTAINED", "3"))
            rmse_mult = float(os.environ.get("AUTO_ROLLBACK_RMSE_MULT", "1.10"))
            min_n = int(os.environ.get("AUTO_ROLLBACK_MIN_N", "20"))

            conn = _db_connect()
            try:
                rows = conn.execute(
                    """
                    SELECT rmse, n
                    FROM validation_points
                    WHERE model_name = ?
                    ORDER BY ts_ms DESC
                    LIMIT ?
                    """,
                    (MODEL_NAME, window),
                ).fetchall()
            finally:
                conn.close()

            if not rows:
                bad_streak = 0
                continue

            rmse_w = 0.0
            n_tot = 0
            for r in rows:
                rmse_val = r[0]
                n_val = r[1]
                if rmse_val is None or n_val is None:
                    continue
                rmse_w += float(rmse_val) * float(n_val)
                n_tot += int(n_val)

            if n_tot < min_n:
                bad_streak = 0
                continue

            cur_rmse = rmse_w / max(1, n_tot)

            if cur_rmse >= champ_rmse * rmse_mult:
                bad_streak += 1
            else:
                bad_streak = 0

            if bad_streak >= sustained:
                try:
                    result = rollback_fn()
                    write_job_history_fn(
                        job_name="auto_rollback",
                        event="rollback",
                        detail=f"rollback executed: {result}",
                        exit_code=None,
                    )
                except Exception:
                    pass
                finally:
                    bad_streak = 0

        except Exception:
            bad_streak = 0
            continue


# ---------------------------------------------------
# EQUITY DRIFT CLASSIFICATION
# ---------------------------------------------------

def detect_sustained_equity_drift(
    con,
    window: int,
    min_warn: int,
    min_crit: int,
):
    """
    Returns: "CRIT", "WARN", or None
    """
    try:
        rows = con.execute(
            """
            SELECT level
            FROM equity_drift
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (window,),
        ).fetchall()
    except Exception:
        return None

    if not rows:
        return None

    levels = [r[0] for r in rows]

    crit_n = sum(1 for l in levels if l == "CRIT")
    warn_n = sum(1 for l in levels if l == "WARN")

    if crit_n >= min_crit:
        return "CRIT"
    if warn_n >= min_warn:
        return "WARN"

    return None


def classify_equity_diff(
    diff_pct: float,
    diff_abs: float,
    warn_pct: float,
    crit_pct: float,
    warn_abs: float,
    crit_abs: float,
):
    if diff_pct is None and diff_abs is None:
        return ("UNKNOWN", "no diff computed")

    ap = abs(float(diff_pct or 0.0))
    aa = abs(float(diff_abs or 0.0))

    if ap >= crit_pct or aa >= crit_abs:
        return ("CRIT", "equity diff exceeds CRIT threshold")
    if ap >= warn_pct or aa >= warn_abs:
        return ("WARN", "equity diff exceeds WARN threshold")

    return ("OK", "equity diff within tolerance")
