import time
import os

from engine.storage import (
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
)

from engine.provider_router import (
    detect_cross_provider_anomalies,
    compute_provider_health,
)

JOB_NAME = "provider_monitor"
OWNER = os.environ.get("JOB_OWNER", "system")
PID = os.getpid()

def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=180):
        raise SystemExit(2)

    try:
        while True:
            detect_cross_provider_anomalies()
            compute_provider_health()
            touch_job_lock(JOB_NAME, OWNER, PID)
            time.sleep(2)
    finally:
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
