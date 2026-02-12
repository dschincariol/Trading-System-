# FILE: universe_discovery_job.py
# NEW FILE (CREATE):

# universe_discovery_job.py
"""
Runs the Universe Discovery Engine once.

Recommended schedule:
  - every 1–5 minutes (live)
  - every 15 minutes (paper)

Writes summary JSON to stdout.
"""

import json
import os
import sys
import time

from dev_core.storage import connect, init_db, acquire_job_lock, release_job_lock
from dev_core.universe_discovery import discover_universe_once

JOB_NAME = "universe_discovery"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()
LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "120"))


def _print(obj):
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
    sys.stdout.flush()


def main() -> int:
    con = connect()
    try:
        init_db()
        if not acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=LOCK_STALE_AFTER_S):
            _print({"ok": True, "status": "locked_out", "job": JOB_NAME})
            return 0

        started = int(time.time() * 1000)
        res = discover_universe_once(con=con, ts_ms=started)
        _print({"ok": True, "status": "done", "job": JOB_NAME, "result": res, "dur_ms": int(time.time() * 1000) - started})
        return 0
    except Exception as e:
        _print({"ok": False, "status": "error", "job": JOB_NAME, "error": str(e)})
        return 2
    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass
        try:
            con.commit()
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
