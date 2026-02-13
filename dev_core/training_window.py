# dev_core/training_window.py
import math
import time
from typing import List, Tuple

from dev_core.storage import connect
from dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot

DAY_MS = 86400 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


def load_training_labels(
    *,
    min_days: int,
    max_days: int,
    halflife_days: int,
) -> List[Tuple]:
    """
    Returns rows from labels table with exponential time decay weights.
    Adds 'sample_weight' column at the end.
    """
    now = _now_ms()
    min_ts = now - (max_days * DAY_MS)
    max_ts = now - (min_days * DAY_MS)

    con = connect()
    try:
        # Fail-soft if labels table isn't created yet
        try:
            con.execute("SELECT 1 FROM labels LIMIT 1").fetchone()
        except Exception:
            return 0

        rows = con.execute(
            """
            SELECT
              l.event_id,
              l.symbol,
              l.horizon_s,
              CASE
            WHEN le.realized=1 THEN le.net_z
            ELSE COALESCE(le.net_z, l.realized_z)
            END AS target_z,
              l.ts_ms
            FROM labels l
            LEFT JOIN labels_exec le
              ON le.event_id=l.event_id AND le.symbol=l.symbol AND le.horizon_s=l.horizon_s
            WHERE l.ts_ms BETWEEN ? AND ?
            AND COALESCE(le.net_z, l.realized_z) IS NOT NULL
            """,
            (int(min_ts), int(max_ts)),
        ).fetchall()

    finally:
        con.close()

    out = []
    for eid, sym, h, z, ts in rows:
        age_days = max(0.0, (now - int(ts)) / DAY_MS)
        # exponential decay: weight halves every halflife_days
        w = math.exp(-math.log(2.0) * age_days / max(1.0, float(halflife_days)))
        out.append((eid, sym, h, z, ts, float(w)))

    return out
