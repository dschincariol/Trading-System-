import os
import sys
import time
import json

from engine.runtime.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

from engine.runtime.alerts import emit_alert


if os.environ.get("ENGINE_SUPERVISED") != "1":
    print("provider_monitor must be launched by supervisor")
    sys.exit(1)


JOB_NAME = "provider_monitor"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

HEARTBEAT_EVERY_S = float(os.environ.get("PROVIDER_MONITOR_HEARTBEAT_S", "10"))
CHECK_EVERY_S = float(os.environ.get("PROVIDER_MONITOR_CHECK_S", "15"))
STALE_AFTER_S = float(os.environ.get("PROVIDER_MONITOR_STALE_AFTER_S", "120"))
LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))


def _check_providers(now_ts_ms: int):
    cutoff = now_ts_ms - int(STALE_AFTER_S * 1000)

    con = connect()
    try:
        rows = con.execute(
            """
            SELECT provider, MAX(ts_ms), MAX(ok)
            FROM price_provider_health
            GROUP BY provider
            """
        ).fetchall()

        for provider, ts_ms, ok in rows or []:
            if ts_ms is None:
                continue

            if int(ts_ms) < cutoff:
                emit_alert(
                    event_title=f"Provider stale: {provider}",
                    symbol=None,
                    horizon_s=0,
                    expected_z=0.0,
                    confidence=1.0,
                    explain={
                        "provider": provider,
                        "last_update_ts_ms": ts_ms,
                        "stale_for_s": int((now_ts_ms - ts_ms) / 1000),
                        "type": "provider_stale",
                    },
                )
            elif ok == 0:
                emit_alert(
                    event_title=f"Provider failing: {provider}",
                    symbol=None,
                    horizon_s=0,
                    expected_z=0.0,
                    confidence=1.0,
                    explain={
                        "provider": provider,
                        "last_update_ts_ms": ts_ms,
                        "type": "provider_fail",
                    },
                )
    finally:
        con.close()


def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        raise SystemExit(2)

    last_hb = 0.0

    try:
        while True:
            now_s = time.time()
            now_ts_ms = int(now_s * 1000)

            if now_s - last_hb >= HEARTBEAT_EVERY_S:
                touch_job_lock(JOB_NAME, OWNER, PID)
                put_job_heartbeat(
                    JOB_NAME,
                    OWNER,
                    PID,
                    extra_json=json.dumps(
                        {"monitor": "provider_health"},
                        separators=(",", ":"),
                    ),
                )
                last_hb = now_s

            _check_providers(now_ts_ms)

            time.sleep(CHECK_EVERY_S)

    finally:
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
