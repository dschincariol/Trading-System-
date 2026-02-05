"""
recalibrate_confidence.py

One-shot job to refresh confidence calibration curves and relevance stats.

Idempotent and safe to run repeatedly.
"""

import os
import time

from dev_core.storage import init_db, acquire_job_lock, release_job_lock
from dev_core.training_guard import training_allowed
from dev_core.embed_regressor import train_embed_models
from dev_core.learning import learn_relevance_stats


def main():
    if not training_allowed():
        print("[training_guard] training disabled")
        raise SystemExit(0)

    init_db()

    if not acquire_job_lock("recalibrate_confidence", ttl_s=30 * 60):
        print("[recalibrate_confidence] locked (already running?)")
        raise SystemExit(0)

    try:
        # ensure embed regressor calibration curves are refreshed
        os.environ["EMBED_CONF_CALIB"] = "1"

        # mirror train_embed_models.py defaults but allow env overrides
        symbols = os.environ.get("EMBED_MODEL_SYMBOLS", "SPY,QQQ,IWM").split(",")
        symbols = [s.strip().upper() for s in symbols if s.strip()]

        horizons = os.environ.get("EMBED_MODEL_HORIZONS_S", "60,300,900").split(",")
        horizons = [int(x.strip()) for x in horizons if x.strip()]

        min_samples = int(os.environ.get("EMBED_MODEL_MIN_SAMPLES", "200"))
        alpha = float(os.environ.get("EMBED_MODEL_RIDGE_ALPHA", "1.0"))
        lookback_days = int(os.environ.get("EMBED_MODEL_LOOKBACK_DAYS", "30"))
        kind = str(os.environ.get("EMBED_MODEL_KIND", "ridge")).strip().lower()
        if kind not in ("ridge", "mlp"):
            kind = "ridge"

        t0 = time.time()
        res = train_embed_models(
            symbols=symbols,
            horizons_s=horizons,
            min_samples=min_samples,
            alpha=alpha,
            lookback_days=lookback_days,
            kind=kind,
        )
        dt = time.time() - t0
        print("[recalibrate_confidence] embed_models:", res, f"dt_s={dt:.2f}")

        # refresh learned relevance stats (read/write logic lives in dev_core.learning)
        try:
            rs = learn_relevance_stats()
            print("[recalibrate_confidence] relevance_stats: ok", ("keys=" + str(len(rs)) if isinstance(rs, dict) else ""))
        except Exception as e:
            print("[recalibrate_confidence] relevance_stats error:", str(e))

    finally:
        try:
            release_job_lock("recalibrate_confidence")
        except Exception:
            pass


if __name__ == "__main__":
    main()
