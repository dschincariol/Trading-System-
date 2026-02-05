# train_model_v2.py

import time
import os
import logging
import socket

from dev_core.storage import connect, init_db
from dev_core.storage import acquire_job_lock, release_job_lock
from dev_core.training_guard import training_allowed
from dev_core.model_v2 import train_regime_stats


# ----------------------------
# Job identity
# ----------------------------
JOB_NAME = "train_model_v2"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", socket.gethostname())),
)
PID = os.getpid()


# ----------------------------
# Logging
# ----------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [train_model_v2] %(message)s",
)


# ----------------------------
# Training config
# ----------------------------
SYMBOLS = ["SPY", "BTC", "OIL"]
HORIZONS = [300, 3600]
LOOKBACK_DAYS = int(os.environ.get("MODEL_V2_LOOKBACK_DAYS", "180"))


# ----------------------------
# Main logic
# ----------------------------
def main() -> int:
    init_db()

    if not training_allowed():
        logging.warning("training blocked by training_guard")
        return 0

    if not acquire_job_lock(JOB_NAME, OWNER, PID):
        logging.error("another training job is running; exiting")
        return 0

    try:
        logging.info(
            "training v2 regime stats symbols=%s horizons=%s lookback_days=%s",
            SYMBOLS,
            HORIZONS,
            LOOKBACK_DAYS,
        )

        n = train_regime_stats(
            SYMBOLS,
            HORIZONS,
            lookback_days=LOOKBACK_DAYS,
        )

        logging.info("trained v2 stats rows_upserted=%s", n)
        return 0

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            logging.exception("failed to release job lock")


# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    raise SystemExit(main())
