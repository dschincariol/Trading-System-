# run_dev.py
"""
WINDOWS-ONLY DEV MODE

Implements:
- Event ingestion
- Price ingestion
- Labeling (ground truth)
- Learning
- Prediction
- Alerting
- Walk-forward validation (STEP 7)

Safety enhancements:
- structured main() with try/except
- explicit transaction rollback on failures
- auto-expiring kill switch via environment variable
- logging of uncaught exceptions
"""

from pathlib import Path
import os
import sys
import traceback
import logging
import numpy as np
from sentence_transformers import SentenceTransformer
import torch

# Explicit CPU threading (important on many-core Ryzen)
torch.set_num_threads(int(os.environ.get("TORCH_NUM_THREADS", "16")))
torch.set_num_interop_threads(int(os.environ.get("TORCH_INTEROP_THREADS", "4")))

from engine.storage import init_db, put_event, put_price, connect
from engine.prices.csv_feed import load_prices
from engine.labeling import label_event
from engine.learning import learn_relevance_stats as train_stats_from_labels

from engine.predictor import expected_impact
from engine.alerts import emit_alert, init_alerts_db
from engine.validation import (
    init_validation_db,
    store_prediction,
    compute_validation_scores,
    get_validation_scores,
)

# configure simple logging to stdout
logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")

EXPIRY_KILL_SWITCH = os.environ.get("DEV_KILL_SWITCH_EXPIRY", "").strip()
if EXPIRY_KILL_SWITCH:
    try:
        EXPIRY_KILL_SWITCH = float(EXPIRY_KILL_SWITCH)
    except ValueError:
        EXPIRY_KILL_SWITCH = None

NEWS = [
    "Federal Reserve signals interest rates may stay higher for longer",
    "Bitcoin surges after ETF approval rumors",
    "Oil prices jump amid Middle East tensions",
    "Tech stocks fall after earnings warnings",
]

SYMBOLS = ["SPY", "BTC", "OIL"]
HORIZONS = [300, 3600]


def kill_switch_active():
    if EXPIRY_KILL_SWITCH is not None:
        # if current time beyond expiry, trigger shutdown
        import time

        if time.time() > EXPIRY_KILL_SWITCH:
            logging.warning("kill switch expired, aborting run")
            return True
    return False


def safe_db_operation(fn, *args, **kwargs):
    con = connect()
    try:
        res = fn(con, *args, **kwargs)
        con.commit()
        return res
    except Exception:
        con.rollback()
        logging.error("database operation failed", exc_info=True)
        raise
    finally:
        con.close()

def main():
    # ---------------------------
    # Boot
    # ---------------------------

    dev = os.environ.get("EMBED_DEVICE", "").strip().lower()
    if not dev:
        dev = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer("all-MiniLM-L6-v2", device=dev)

    init_db()
    init_alerts_db()
    init_validation_db()

    # ---------------------------
    # Load Prices
    # ---------------------------

    price_data = load_prices(Path("data/prices.csv"))
    for sym, series in price_data.items():
        for p in series:
            safe_db_operation(lambda conn: conn.execute(
                "INSERT OR REPLACE INTO prices(ts_ms, symbol, price) VALUES (?, ?, ?)",
                (p["ts_ms"], sym, p["price"])
            ))

    # early kill-switch check
    if kill_switch_active():
        return

    # ---------------------------
    # Anchor Events
    # ---------------------------

    base_ts_ms = min(
        p["ts_ms"]
        for series in price_data.values()
        for p in series
    )

    event_rows = []
    for i, title in enumerate(NEWS):
        ts = base_ts_ms + (i + 1) * 1000
        eid = safe_db_operation(lambda conn: conn.execute(
            "INSERT OR REPLACE INTO events(ts_ms, source, title, body, url, event_key) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, "dev", title, "", "", f"dev:{i}:{abs(hash(title)) % 10_000_000}")
        ) or conn.lastrowid)
        # conn.execute returns cursor, so grab lastrowid after
        event_rows.append((eid, ts, title))

    if kill_switch_active():
        return

    # ---------------------------
    # Embed + Store
    # ---------------------------

    embeddings = model.encode([t for _, _, t in event_rows]).astype(np.float32)

    def store_embeddings(conn):
        cur = conn.cursor()
        for (eid, _, _), vec in zip(event_rows, embeddings):
            cur.execute(
                """
                INSERT OR REPLACE INTO event_embeddings(event_id, dim, vec)
                VALUES (?, ?, ?)
                """,
                (eid, len(vec), vec.tobytes()),
            )
    safe_db_operation(store_embeddings)

    # ---------------------------
    # Label events (ground truth)
    # ---------------------------

    for eid, ets, _ in event_rows:
        try:
            label_event(eid, ets, price_data)
        except Exception:
            logging.error("label_event failed", exc_info=True)

    if kill_switch_active():
        return

    # ---------------------------
    # Train model
    # ---------------------------

    try:
        train_stats_from_labels()
    except Exception:
        logging.error("train_stats_from_labels failed", exc_info=True)

    if kill_switch_active():
        return

    # ---------------------------
    # Predict → Store → Alert
    # ---------------------------

    logging.info("Predictions + alerts:")

    for eid, _, title in event_rows:
        if kill_switch_active():
            break
        qvec = model.encode([title])[0]

        logging.info(f"EVENT: {title}")
        for sym in SYMBOLS:
            for h in HORIZONS:
                try:
                    z, conf = expected_impact(qvec, sym, h)
                    logging.info(f"  {sym} h={h} predicted_z={z:+.3f} conf={conf:.2f}")

                    safe_db_operation(lambda conn: conn.execute(
                        "INSERT OR REPLACE INTO predictions(event_id, symbol, horizon_s, zscore, confidence) VALUES (?, ?, ?, ?, ?)",
                        (eid, sym, h, z, conf),
                    ))

                    emit_alert(
                        event_title=title,
                        symbol=sym,
                        horizon_s=h,
                        expected_z=z,
                        confidence=conf,
                    )
                except Exception:
                    logging.error("prediction/alert loop failed", exc_info=True)

        logging.info("-" * 60)

    # ---------------------------
    # Validate (compare predictions vs realized)
    # ---------------------------

    try:
        validated = compute_validation_scores()
    except Exception:
        logging.error("compute_validation_scores failed", exc_info=True)
        validated = None

    logging.info("Validation scores:")
    for sym, h, mae, rmse, n, ts in get_validation_scores():
        logging.info(f"  {sym} h={h} MAE={mae:.3f} RMSE={rmse:.3f} n={n}")

    logging.info("DEV RUN COMPLETE")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.critical("unhandled exception in run_dev", exc_info=True)
        sys.exit(1)
# Validate (compare predictions vs realized)
# ---------------------------

validated = compute_validation_scores()

print("\nValidation scores:")
for sym, h, mae, rmse, n, ts in get_validation_scores():
    print(f"  {sym} h={h} MAE={mae:.3f} RMSE={rmse:.3f} n={n}")

print("\nDEV RUN COMPLETE\n")
