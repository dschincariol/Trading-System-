# pipeline_runner.py
import time

# -------------------------------------------------
# Last-run timestamps (process local, read-only)
# -------------------------------------------------

LAST_AUTO_PIPELINE_TS = None
LAST_AUTO_PIPELINE_HEARTBEAT_TS = None

LAST_AUTO_CHALLENGER_TS = None
LAST_AUTO_CHALLENGER_HEARTBEAT_TS = None

LAST_AUTO_SIZE_POLICY_TS = None
LAST_AUTO_SIZE_POLICY_HEARTBEAT_TS = None

from dev_core.storage import connect as _db_connect

from dashboard_config import (
    PIPELINE_ORDER,
    AUTO_PIPELINE_INCLUDE_EXECUTION,
    AUTO_PIPELINE_START_DELAY_S,
    AUTO_PIPELINE_INTERVAL_S,
    AUTO_PIPELINE_LOG,
    AUTO_CHALLENGER_START_DELAY_S,
    AUTO_CHALLENGER_INTERVAL_S,
    AUTO_CHALLENGER_LOG,
    AUTO_CHALLENGER_MIN_DRIFT,
    AUTO_SIZE_POLICY_START_DELAY_S,
    AUTO_SIZE_POLICY_INTERVAL_S,
    AUTO_SIZE_POLICY_LOG,
)

from jobs_manager import _acquire_lock, _release_lock


# -------------------------------------------------
# PIPELINE RUNNER
# -------------------------------------------------

def run_pipeline(JOBS):
    if not _acquire_lock("pipeline", ttl_ms=20 * 60 * 1000):
        return {"ok": False, "error": "pipeline locked"}

    try:
        if not (
    JOBS.is_running("poll_prices")
    or JOBS.is_running("stream_prices_polygon_ws")
    or JOBS.is_running("stream_prices_ibkr")
):

            return {"ok": False, "error": "prices daemon must be running (poll_prices or stream_prices_polygon_ws)"}

        for name in PIPELINE_ORDER:
            if name in ("portfolio_rebalance", "broker_apply_orders") and not AUTO_PIPELINE_INCLUDE_EXECUTION:
                continue

            job = JOBS.get(name)
            if not job or job.mode == "daemon":
                continue

            res = JOBS.start(name)
            if not res.get("ok"):
                return {"ok": False, "error": f"{name}: {res.get('error') or res.get('reason')}"}

            start_ts = time.time()
            while True:
                time.sleep(0.25)

                if not job.proc:
                    break

                if job.proc.poll() is not None:
                    if job.exit_code not in (0, None):
                        return {"ok": False, "error": f"{name} exited rc={job.exit_code}"}
                    break

                if time.time() - start_ts > 20 * 60:
                    return {"ok": False, "error": f"{name} timed out"}

        return {"ok": True}

    finally:
        _release_lock("pipeline")


# -------------------------------------------------
# AUTO PIPELINE LOOP
# -------------------------------------------------

def auto_pipeline_loop(JOBS):
    global LAST_AUTO_PIPELINE_TS

    time.sleep(max(0.0, float(AUTO_PIPELINE_START_DELAY_S)))

    while True:
        LAST_AUTO_PIPELINE_HEARTBEAT_TS = int(time.time())
        try:
            LAST_AUTO_PIPELINE_TS = int(time.time())

            res = run_pipeline(JOBS)

            if AUTO_PIPELINE_LOG:
                print("[auto_pipeline]", res)
        except Exception as e:
            if AUTO_PIPELINE_LOG:
                print("[auto_pipeline] ERROR:", e)

        time.sleep(max(5.0, float(AUTO_PIPELINE_INTERVAL_S)))


# -------------------------------------------------
# AUTO CHALLENGER LOOP
# -------------------------------------------------

def _max_drift_ratio():
    con = _db_connect()
    try:
        row = con.execute("SELECT MAX(drift_ratio) FROM model_drift").fetchone()
        return float(row[0] or 0.0) if row else 0.0
    finally:
        con.close()

def auto_challenger_loop(JOBS):
    global LAST_AUTO_CHALLENGER_TS

    time.sleep(max(0.0, float(AUTO_CHALLENGER_START_DELAY_S)))

    while True:
        try:
            md = _max_drift_ratio()
            if AUTO_CHALLENGER_MIN_DRIFT > 0.0 and md < AUTO_CHALLENGER_MIN_DRIFT:
                if AUTO_CHALLENGER_LOG:
                    print(f"[auto_challenger] skipped drift={md:.3f}")
            else:
                LAST_AUTO_CHALLENGER_TS = int(time.time())
                res = JOBS.start("train_and_eval_challenger")

                if AUTO_CHALLENGER_LOG:
                    print("[auto_challenger]", res)
        except Exception as e:
            if AUTO_CHALLENGER_LOG:
                print("[auto_challenger] ERROR:", e)

        time.sleep(max(30.0, float(AUTO_CHALLENGER_INTERVAL_S)))


# -------------------------------------------------
# AUTO SIZE POLICY LOOP (RE-ENABLED)
# -------------------------------------------------

def auto_size_policy_loop(JOBS):
    global LAST_AUTO_SIZE_POLICY_TS

    time.sleep(max(0.0, float(AUTO_SIZE_POLICY_START_DELAY_S)))

    while True:
        try:
            LAST_AUTO_SIZE_POLICY_TS = int(time.time())
            res = JOBS.start("train_size_policy")

            if AUTO_SIZE_POLICY_LOG:
                print("[auto_size_policy]", res)
        except Exception as e:
            if AUTO_SIZE_POLICY_LOG:
                print("[auto_size_policy] ERROR:", e)

        time.sleep(max(60.0, float(AUTO_SIZE_POLICY_INTERVAL_S)))
