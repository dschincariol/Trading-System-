# pipeline_train_and_eval.py
import os
import time
import math
import subprocess
import json
import logging
import random
from typing import Dict, Any, Optional, Tuple

from engine.dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
    put_event,
)
# from dashboard_server import get_health_snapshot  # unused

from engine.dev_core.model_registry import register_model, get_stage_latest, promote_to_champion
from engine.dev_core.promotion_hardening import promote_with_snapshot_and_db_watch
from engine.dev_core.promotion_guard import promotion_allowed
from engine.dev_core.promotion_audit import audit
from engine.dev_core.training_guard import training_allowed


# ------            -- ------------------------------------------------------
# Training guard
# ------            -- ------------------------------------------------------
if not training_allowed():
    print("[training_guard] training disabled")
    raise SystemExit(0)


# ------            -- ------------------------------------------------------
# Constants
# ------            -- ------------------------------------------------------
MODEL_NAME = os.environ.get("MODEL_NAME", "embed_regressor")
ACTIVE_REGIMES = ["global", "low_vol", "high_vol", "trend", "shock"]

JOB_NAME = "pipeline_train_and_eval"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "20.0"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [pipeline_train_and_eval] %(message)s",
)

# Promotion thresholds
PROMOTE_MIN_IMPROVEMENT = float(os.environ.get("PROMOTE_MIN_IMPROVEMENT", "0.01"))
PROMOTE_DIRACC_TOL = float(os.environ.get("PROMOTE_DIRACC_TOL", "0.00"))
EVAL_LIMIT_ROWS = int(os.environ.get("CHALLENGER_EVAL_LIMIT_ROWS", "5000"))


# ------            -- ------------------------------------------------------
# Small helpers
# ------            -- ------------------------------------------------------
def _sleep_with_jitter(seconds: float) -> None:
    if seconds <= 0:
        return
    j = seconds * 0.2
    time.sleep(max(0.05, seconds + random.uniform(-j, j)))


def _latest_age_s(con, table: str) -> Optional[float]:
    try:
        row = con.execute(f"SELECT MAX(ts_ms) FROM {table}").fetchone()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    return (int(time.time() * 1000) - int(row[0])) / 1000.0


def _count(con, table: str) -> int:
    try:
        row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0] or 0)
    except Exception:
        return 0


def _run_python(script: str) -> int:
    p = subprocess.run(
        ["python", script],
        capture_output=True,
        text=True,
    )
    if p.stdout:
        print(p.stdout.strip())
    if p.returncode != 0 and p.stderr:
        print(p.stderr.strip())
    return int(p.returncode)


# ------            -- ------------------------------------------------------
# Data-quality gates (fail-closed)
# ------            -- ------------------------------------------------------
def _data_gates_or_exit() -> None:
    con = connect()
    try:
        pred_age_s = _latest_age_s(con, "predictions")
        lbl_age_s = _latest_age_s(con, "labels")
        lbl_n = _count(con, "labels")
    finally:
        con.close()

    MAX_PRED_AGE_S = float(os.environ.get("TRAIN_MAX_PREDICTIONS_AGE_S", "900"))
    MAX_LBL_AGE_S = float(os.environ.get("TRAIN_MAX_LABELS_AGE_S", "900"))
    MIN_LABELS = int(os.environ.get("TRAIN_MIN_LABELS", "50"))

    if pred_age_s is None or pred_age_s > MAX_PRED_AGE_S:
        logging.error("ABORT: predictions stale or missing age_s=%s limit=%s", pred_age_s, MAX_PRED_AGE_S)
        raise SystemExit(3)

    if lbl_age_s is None or lbl_age_s > MAX_LBL_AGE_S:
        logging.error("ABORT: labels stale or missing age_s=%s limit=%s", lbl_age_s, MAX_LBL_AGE_S)
        raise SystemExit(3)

    if lbl_n < MIN_LABELS:
        logging.error("ABORT: insufficient labels count=%s min=%s", lbl_n, MIN_LABELS)
        raise SystemExit(3)

    logging.info(
        "data gates OK predictions_age_s=%.1f labels_age_s=%.1f labels_n=%s",
        float(pred_age_s),
        float(lbl_age_s),
        int(lbl_n),
    )


# ------            -- ------------------------------------------------------
# Net-of-cost evaluation (execution-aware)
# ------            -- ------------------------------------------------------
def _net_eval_metrics(con, lookback_days: int = 90) -> Optional[Dict[str, Any]]:
    DAY_MS = 86400 * 1000
    now = int(time.time() * 1000)
    min_ts = now - int(lookback_days) * DAY_MS

    rows = con.execute(
        """
        SELECT p.predicted_z, le.net_z
        FROM predictions p
        JOIN labels_exec le
          ON le.event_id=p.event_id
         AND le.symbol=p.symbol
         AND le.horizon_s=p.horizon_s
        WHERE p.ts_ms >= ?
          AND le.net_z IS NOT NULL
        LIMIT 50000
        """,
        (int(min_ts),),
    ).fetchall()

    if not rows or len(rows) < 200:
        return None

    n = 0
    se = 0.0
    hit = 0

    for pred, netz in rows:
        try:
            pr = float(pred)
            nz = float(netz)
        except Exception:
            continue
        n += 1
        se += (pr - nz) ** 2
        if (pr >= 0 and nz >= 0) or (pr < 0 and nz < 0):
            hit += 1

    if n <= 0:
        return None

    rmse = math.sqrt(se / n)
    diracc = float(hit) / float(n)

    return {
        "n_eval_net": n,
        "rmse_net": rmse,
        "directional_acc_net": diracc,
    }


# ------            -- ------------------------------------------------------
# Embed model eval aggregation
# ------            -- ------------------------------------------------------
def _latest_embed_eval_snapshot(con) -> int:
    r = con.execute("SELECT MAX(ts_ms) FROM embed_model_eval").fetchone()
    return int(r[0] or 0)


def _load_embed_eval_rows(con, ts_ms: int):
    return con.execute(
        """
        SELECT model_kind, n_eval, rmse, directional_acc
        FROM embed_model_eval
        WHERE ts_ms=?
        LIMIT ?
        """,
        (int(ts_ms), int(EVAL_LIMIT_ROWS)),
    ).fetchall()


def _aggregate(rows) -> Tuple[str, Dict[str, Any]]:
    if not rows:
        return ("unknown", {"n_eval": 0, "rmse": None, "directional_acc": None})

    kinds = {}
    sum_w = 0.0
    sum_mse = 0.0
    sum_dir = 0.0

    for mk, n_eval, rmse, diracc in rows:
        w = float(n_eval or 0)
        if w <= 0:
            continue
        kinds[mk] = kinds.get(mk, 0) + w
        try:
            r = float(rmse)
            d = float(diracc)
        except Exception:
            continue
        sum_w += w
        sum_mse += w * (r * r)
        sum_dir += w * d

    kind = max(kinds.items(), key=lambda x: x[1])[0] if kinds else "unknown"
    if sum_w <= 0:
        return (kind, {"n_eval": 0, "rmse": None, "directional_acc": None})

    return (
        kind,
        {
            "n_eval": int(sum_w),
            "rmse": math.sqrt(sum_mse / sum_w),
            "directional_acc": sum_dir / sum_w,
        },
    )


# ------            -- ------------------------------------------------------
# Challenger vs champion comparison
# ------            -- ------------------------------------------------------
def _beats_champion(candidate: Dict[str, Any], champion: Optional[Dict[str, Any]]):
    if champion is None:
        return True, {"no_champion": True}

    c_rmse = candidate.get("rmse_net", candidate.get("rmse"))
    c_dir = candidate.get("directional_acc_net", candidate.get("directional_acc"))

    chm = champion.get("metrics") or {}
    ch_rmse = chm.get("rmse_net", chm.get("rmse"))
    ch_dir = chm.get("directional_acc_net", chm.get("directional_acc"))

    if None in (c_rmse, c_dir, ch_rmse, ch_dir):
        return False, {"missing_metrics": True}

    rel_ok = c_rmse < ch_rmse * (1.0 - PROMOTE_MIN_IMPROVEMENT)
    dir_ok = c_dir >= ch_dir - PROMOTE_DIRACC_TOL

    return bool(rel_ok and dir_ok), {
        "candidate_rmse": c_rmse,
        "champion_rmse": ch_rmse,
        "candidate_dir": c_dir,
        "champion_dir": ch_dir,
        "rel_ok": rel_ok,
        "dir_ok": dir_ok,
    }


# ------            -- ------------------------------------------------------
# Main pipeline
# ------            -- ------------------------------------------------------
def main() -> int:
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        return 2

    started_ms = int(time.time() * 1000)
    last_hb_s = 0.0

    try:
        # 0) Data quality gates
        _data_gates_or_exit()

        # heartbeat
        now_s = time.time()
        touch_job_lock(JOB_NAME, OWNER, PID)
        put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"phase": "start"}))
        last_hb_s = now_s

        # 1) Train models
        rc = _run_python("train_embed_models.py")
        if rc != 0:
            return rc

        # 1.5) Evaluate temporal shadow models (A.7)
        _run_python("jobs/eval_temporal_shadow.py")

        if rc != 0:
            audit(
                actor="system",
                action="block",
                model_name=MODEL_NAME,
                reason={"error": "train_embed_models_failed", "returncode": int(rc)},
            )
            return int(rc)

        # 2) Load latest eval snapshot + rows
        con = connect()
        try:
            snap = _latest_embed_eval_snapshot(con)
            if not snap:
                audit(
                    actor="system",
                    action="block",
                    model_name=MODEL_NAME,
                    reason={"error": "no_embed_model_eval"},
                )
                return 1
            rows = _load_embed_eval_rows(con, snap)
        finally:
            con.close()

        kind, metrics = _aggregate(rows)
        metrics.update(
            {
                "eval_ts_ms": int(snap),
                "pipeline_started_ms": int(started_ms),
                "pipeline_finished_ms": int(time.time() * 1000),
            }
        )

        # 3) Net-of-cost evaluation (optional)
        con = connect()
        try:
            nem = _net_eval_metrics(con, lookback_days=90)
        finally:
            con.close()
        if nem:
            metrics.update(nem)

        # 4) Register challenger
        register_model(
            model_name=MODEL_NAME,
            model_kind=kind,
            model_ts_ms=int(snap),
            stage="challenger",
            metrics=metrics,
            note="pipeline_train_and_eval",
        )
        # -----------------------------
        # A.10 Temporal promotion
        # NOTE: temporal models are stored in temporal_models tables, not model_registry.
        # Promotion must be handled by a dedicated temporal promotion script (A.10),
        # not via promote_to_champion() which operates on model_registry.
        # -----------------------------

        # 5) Promotion guards
        allowed, guard_reason = promotion_allowed()
        if not allowed:
            audit(
                actor="auto",
                action="block",
                model_name=MODEL_NAME,
                to_kind=kind,
                to_ts_ms=int(snap),
                reason={"guard": guard_reason, "metrics": metrics},
            )
            print("[BLOCKED] promotion guards:", guard_reason)
            return 0

        # 6) Compare to champion
        champion = get_stage_latest(MODEL_NAME, "champion")
        ok, cmp_reason = _beats_champion(metrics, champion)
        if not ok:
            audit(
                actor="auto",
                action="reject",
                model_name=MODEL_NAME,
                from_kind=(champion.get("model_kind") if champion else None),
                from_ts_ms=(champion.get("model_ts_ms") if champion else None),
                to_kind=kind,
                to_ts_ms=int(snap),
                reason={"compare": cmp_reason, "guard": guard_reason, "metrics": metrics},
            )
            print("[REJECT] challenger did not beat champion:", cmp_reason)
            return 0

        # 7) Promote globally + per regime
        prev_kind = champion.get("model_kind") if champion else None
        prev_ts = champion.get("model_ts_ms") if champion else None

        # Regime-specific promotion with label sufficiency gate
        for regime in ACTIVE_REGIMES:
            now_s = time.time()
            if (now_s - last_hb_s) >= HEARTBEAT_EVERY_S:
                touch_job_lock(JOB_NAME, OWNER, PID)

            put_job_heartbeat(
                JOB_NAME,
                OWNER,
                PID,
                extra_json=json.dumps({"phase": "promote", "regime": regime}),
            )
            last_hb_s = now_s

            # Ensure regime has enough labeled support
            try:
                con = connect()
                try:
                    row = con.execute(
                        """
                        SELECT COUNT(*)
                        FROM labels
                        WHERE impact_z IS NOT NULL
                        AND regime = ?
                        """,
                        (str(regime),),
                    ).fetchone()
                    n_reg = int(row[0] or 0)
                finally:
                    con.close()
            except Exception:
                n_reg = 0

            min_reg = int(os.environ.get("PROMOTE_MIN_REGIME_LABELS", "50"))
            if n_reg < min_reg:
                print(f"[PROMOTE] skip regime={regime} n_reg={n_reg} < min_reg={min_reg}")
                continue

            promote_with_snapshot_and_db_watch(
                MODEL_NAME,
                kind,
                int(snap),
                key=str(regime),
                actor="auto",
                extra_reason={"compare": cmp_reason, "guard": guard_reason, "metrics": metrics},
            )

            print(f"[PROMOTE] promoted challenger -> champion key={regime}")

        audit(
            actor="auto",
            action="promote",
            model_name=MODEL_NAME,
            from_kind=prev_kind,
            from_ts_ms=prev_ts,
            to_kind=kind,
            to_ts_ms=int(snap),
            reason={"compare": cmp_reason, "guard": guard_reason, "metrics": metrics},
        )
        print(f"[PROMOTE] champion <- {kind} @ {int(snap)}")
        return 0

    except SystemExit:
        raise
    except Exception as e:
        logging.exception("pipeline failed: %r", e)
        _sleep_with_jitter(5.0)
        raise
    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
