# train_embed_models.py
"""
A.1 Scheduled retraining helper (ONESHOT, idempotent).

- Trains supervised embedding models (Option A) only when there are enough new labels.
- Stores last-trained label stamp in SQLite so it can be safely called often.

Exit code:
  0 = ok (trained or skipped)
  nonzero = failed
"""

import os
import time
import socket
import os as _os

from engine.dev_core.storage import connect, init_db, acquire_job_lock, release_job_lock
from engine.dev_core.embed_regressor import train_embed_models
from engine.dev_core.training_guard import training_allowed

# ----------------------------
# Job identity
# ----------------------------
OWNER = socket.gethostname()
PID = _os.getpid()


# ----------------------------
# Training config
# ----------------------------
SYMBOLS = ["SPY", "BTC", "OIL"]
HORIZONS = [300, 3600]

MIN_NEW_LABELS = int(os.environ.get("EMBED_MODEL_MIN_NEW_LABELS", "25"))
LOOKBACK_DAYS = int(os.environ.get("EMBED_MODEL_LOOKBACK_DAYS", "365"))
ALPHA = float(os.environ.get("EMBED_MODEL_ALPHA", "1.0"))
MIN_SAMPLES = int(os.environ.get("EMBED_MODEL_MIN_SAMPLES", "50"))
MODEL_KIND = os.environ.get("EMBED_MODEL_KIND", "ridge").strip().lower()  # ridge | mlp | auto


# ----------------------------
# DB helpers
# ----------------------------
def _ensure_meta(con):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS model_runs (
          key TEXT PRIMARY KEY,
          last_count INTEGER NOT NULL,
          last_max_created_at_ms INTEGER NOT NULL,
          last_run_ms INTEGER NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_model_runs_last_run ON model_runs(last_run_ms)"
    )


def _labels_stamp(con):
    row = con.execute(
        """
        SELECT COUNT(*), MAX(created_at_ms)
        FROM labels
        WHERE impact_z IS NOT NULL
        """
    ).fetchone()
    n = int((row[0] or 0) if row else 0)
    mx = int((row[1] or 0) if row else 0)
    return n, mx


# ----------------------------
# Main logic
# ----------------------------
def main() -> int:
    init_db()

    if not training_allowed():
        print("embed_models: training disabled by training_guard")
        return 0

    if not acquire_job_lock("train_embed_models", OWNER, PID):
        print("embed_models: another training job is running; exiting")
        return 0

    try:
        con = connect()
        try:
            _ensure_meta(con)

            cur_n, cur_mx = _labels_stamp(con)

            row = con.execute(
                """
                SELECT last_count, last_max_created_at_ms
                FROM model_runs
                WHERE key=?
                """,
                ("embed_models",),
            ).fetchone()

            last_n = int(row[0]) if row else 0
            last_mx = int(row[1]) if row else 0

            new_labels = max(0, cur_n - last_n)
            changed = (cur_mx != last_mx)

            if (not changed) or (new_labels < MIN_NEW_LABELS):
                print(
                    f"embed_models: SKIP cur_n={cur_n} last_n={last_n} "
                    f"new={new_labels} cur_mx={cur_mx} last_mx={last_mx} "
                    f"min_new={MIN_NEW_LABELS}"
                )

                con.execute(
                    """
                    INSERT INTO model_runs(key, last_count, last_max_created_at_ms, last_run_ms)
                    VALUES(?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET
                      last_count=excluded.last_count,
                      last_max_created_at_ms=excluded.last_max_created_at_ms,
                      last_run_ms=excluded.last_run_ms
                    """,
                    ("embed_models", last_n, last_mx, int(time.time() * 1000)),
                )
                con.commit()
                return 0

        finally:
            con.close()

        print(
            f"embed_models: TRAIN cur_n={cur_n} last_n={last_n} new={new_labels} "
            f"lookback_days={LOOKBACK_DAYS} min_samples={MIN_SAMPLES} "
            f"alpha={ALPHA} kind={MODEL_KIND} "
            f"conf_calib={os.environ.get('EMBED_CONF_CALIB','1')} "
            f"conf_k={os.environ.get('EMBED_REGRESSOR_CONF_K','75.0')}"
        )

        # Perform training (does its own DB writes)
        _out = train_embed_models(
            symbols=SYMBOLS,
            horizons=HORIZONS,
            min_samples=MIN_SAMPLES,
            alpha=ALPHA,
            lookback_days=LOOKBACK_DAYS,
            kind=MODEL_KIND,
        )

        # Update meta after training
        con2 = connect()
        try:
            _ensure_meta(con2)
            cur_n2, cur_mx2 = _labels_stamp(con2)
            con2.execute(
                """
                INSERT INTO model_runs(key, last_count, last_max_created_at_ms, last_run_ms)
                VALUES(?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                  last_count=excluded.last_count,
                  last_max_created_at_ms=excluded.last_max_created_at_ms,
                  last_run_ms=excluded.last_run_ms
                """,
                ("embed_models", int(cur_n2), int(cur_mx2), int(time.time() * 1000)),
            )
            con2.commit()
        finally:
            con2.close()

        return 0

    finally:
        try:
            release_job_lock("train_embed_models", OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

# ----------------------------
# Entrypoint
# ----------------------------
if __name__ == "__main__":
    raise SystemExit(main())
