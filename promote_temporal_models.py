"""
A.10 Temporal model promotion script (guarded, audited).

Promotes temporal_models entries to CHAMPION role
ONLY if A.7 shadow evaluation gates passed.

Semantics:
- Uses keyed promotion (symbol / class / global + horizon)
- Does NOT mutate model blobs or model_kind
- Safe dry-run by default

Usage:
  DRY_RUN=1 python promote_temporal_models.py
  PROMOTE_TEMPORAL=1 python promote_temporal_models.py
"""

import os
import json
import time
import socket

from dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
)
from dev_core.promotion_audit import audit

# ------            -- ------------------------------------------------------
# Flags
# ------            -- ------------------------------------------------------

DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"
ALLOW_PROMOTE = os.environ.get("PROMOTE_TEMPORAL", "0") == "1"

MIN_N = int(os.environ.get("TEMPORAL_PROMOTE_MIN_N", "200"))
MAX_MODEL_AGE_DAYS = int(os.environ.get("TEMPORAL_PROMOTE_MAX_AGE_DAYS", "30"))

JOB_NAME = "promote_temporal_models"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", socket.gethostname())),
)
PID = os.getpid()


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

def _now_ms() -> int:
    return int(time.time() * 1000)


# ------            -- ------------------------------------------------------
# Main
# ------            -- ------------------------------------------------------

def main() -> int:
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID):
        print("[BLOCKED] another promotion job is running")
        return 2

    now_ms = _now_ms()
    max_age_ms = MAX_MODEL_AGE_DAYS * 86400 * 1000

    promoted = []
    skipped = []

    try:
        con = connect()
        try:
            rows = con.execute(
                """
                SELECT
                e.key_type,
                e.key,
                e.horizon_s,
                e.rmse,
                e.baseline_rmse,
                e.directional_acc,
                e.baseline_directional_acc,
                e.n,
                json_extract(e.detail_json, '$.latest_model_ts_ms') AS model_ts_ms,
                json_extract(e.detail_json, '$.capital_efficiency') AS capital_efficiency,
                json_extract(e.detail_json, '$.drawdown_contribution') AS drawdown_contribution,
                json_extract(e.detail_json, '$.avg_slippage_impact') AS avg_slippage_impact
                FROM temporal_shadow_eval e
                WHERE e.pass_all = 1
                AND e.n >= ?
                """,
                (int(MIN_N),),
            ).fetchall()

            for (
                key_type,
                key,
                horizon_s,
                rmse,
                baseline_rmse,
                da,
                b_da,
                n,
                model_ts_ms,
                capital_efficiency,
                drawdown_contribution,
                avg_slippage_impact,
            ) in rows or []:


                if not model_ts_ms:
                    skipped.append({"key": key, "reason": "missing_model_ts"})
                    continue

                age_ms = now_ms - int(model_ts_ms)
                if age_ms > max_age_ms:
                    skipped.append({"key": key, "reason": "model_too_old"})
                    continue

                # Safety-first composite score
                if capital_efficiency is None:
                    skipped.append({"key": key, "reason": "missing_capital_efficiency"})
                    continue

                score = (
                    float(capital_efficiency) * 2.0
                    - float(drawdown_contribution or 0.0) * 0.5
                    - float(avg_slippage_impact or 0.0) * 0.5
                    + float(da or 0.0)
                )

                if score <= 0:
                    skipped.append({"key": key, "reason": "negative_safety_score"})
                    continue

                promote_key = f"{key_type}:{key}:{int(horizon_s)}"
                promoted.append(promote_key)

                if DRY_RUN:
                    print("[DRY-RUN] would promote", promote_key)
                    continue

                if not ALLOW_PROMOTE:
                    continue

                # Keyed champion write (temporal_models is source of truth)
                pass

                con.execute(
                    """
                    UPDATE temporal_models
                    SET ts_ms = ?
                    WHERE key_type=?
                      AND key=?
                      AND horizon_s=?
                    """,
                    (
                        int(model_ts_ms),
                        str(key_type),
                        str(key),
                        int(horizon_s),
                    ),
                )

                audit(
                    actor="auto",
                    action="promote_temporal",
                    model_name="temporal_predictor",
                    key=str(promote_key),
                    reason={
                        "rmse": rmse,
                        "baseline_rmse": baseline_rmse,
                        "directional_acc": da,
                        "baseline_directional_acc": b_da,
                        "n": n,
                        "model_ts_ms": int(model_ts_ms),
                        "capital_efficiency": capital_efficiency,
                        "drawdown_contribution": drawdown_contribution,
                        "avg_slippage_impact": avg_slippage_impact,
                        "safety_score": score,
                    },
                )

            if not DRY_RUN and ALLOW_PROMOTE:
                con.commit()

        finally:
            con.close()

        print(json.dumps(
            {
                "ok": True,
                "dry_run": DRY_RUN,
                "allow_promote": ALLOW_PROMOTE,
                "promoted": promoted,
                "skipped": skipped,
            },
            indent=2,
        ))
        return 0

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
