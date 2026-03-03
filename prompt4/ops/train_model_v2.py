# train_model_v2.py

import time
import os
import logging
import socket

from engine.storage import connect, init_db
from engine.storage import acquire_job_lock, release_job_lock
from engine.training_guard import training_allowed
from engine.model_v2 import train_regime_stats


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

    # obey external retraining signals as override
    try:
        from engine.strategy.training_hooks import fetch_pending, mark_processed
        pending = fetch_pending(os.environ.get("MODEL_NAME", "embed_regressor"))
        if pending:
            logging.warning("train_model_v2: forced run due to signals=%s", pending)
            for mname, reason, ts in pending:
                mark_processed(mname, reason)
    except Exception:
        pass

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
