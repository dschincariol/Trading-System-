# compute_options_skew.py
"""
Compute 25-delta options skew execution features.
Improves sizing and execution conditioning (not signal direction).
"""

import os
import math
import logging
import numpy as np

from engine.dev_core.storage import connect, init_db
from engine.dev_core.factor_universe import put_factor_feature

LOG = logging.getLogger("compute_options_skew")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

_ZWIN = 240


def _zscore(xs, win):
    xs = np.asarray(xs, dtype=float)
    if xs.size < max(30, win):
        return 0.0
    w = xs[-win:]
    mu = float(np.mean(w))
    sd = float(np.std(w))
    if sd <= 1e-9:
        return 0.0
    return float((xs[-1] - mu) / sd)


def main():
    init_db()
    con = connect()

    rows = con.execute("""
        SELECT ts_ms, skew_25d
        FROM options_surface
        WHERE skew_25d IS NOT NULL
        ORDER BY ts_ms ASC
    """).fetchall()

    if not rows:
        return

    ts = np.array([int(r[0]) for r in rows])
    skew = np.array([float(r[1]) for r in rows])

    z = _zscore(skew, _ZWIN)

    d5 = 0.0
    if skew.size > 5:
        d5 = float(skew[-1] - skew[-6])

    now = int(ts[-1])

    put_factor_feature(
        con,
        feature_id="options.skew_25d_z",
        asof_ts=now,
        effective_ts=now,
        value=z,
        meta={}
    )

    put_factor_feature(
        con,
        feature_id="options.skew_25d_d5",
        asof_ts=now,
        effective_ts=now,
        value=d5,
        meta={}
    )

    con.commit()


if __name__ == "__main__":
    main()
