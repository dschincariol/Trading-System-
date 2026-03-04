# dev_core/labeling.py
import time
import json
from typing import Dict, List
from engine.prices.returns import compute_return
from engine.prices.volatility import compute_volatility
from engine.storage import connect
from engine.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from engine.model_v2 import classify_regime
from engine.horizons import horizons_s
from engine.horizons import get_spec

HORIZONS_S = horizons_s()

def label_event(
    event_id: int,
    event_ts: int,
    price_series: Dict[str, List[dict]],
):
    con = connect()
    try:
        cur = con.cursor()
        now_ms = int(time.time() * 1000)

        for sym, series in price_series.items():
            vol = float(compute_volatility(series) or 1e-6)
            regime = classify_regime(vol)

            for h_s in HORIZONS_S:
                ret = compute_return(series, event_ts, h_s * 1000)
                if ret is None:
                    continue

                impact_z = float(ret) / float(vol)

                spec = get_spec(int(h_s))
                label_meta = {
                    "label_version": 1,
                    "horizon_name": (spec.name if spec else None),
                    "label_price_lag_ms": (int(spec.label_price_lag_ms) if spec else 0),
                    "max_expected_hold_s": (int(spec.max_expected_hold_s) if spec else None),
                    "source": "prices",
                }

                cur.execute(
                    """
                    INSERT OR IGNORE INTO labels(
                      event_id, horizon_s, symbol,
                      baseline_ret, realized_ret,
                      impact_z, created_at_ms,
                      vol_proxy, regime,
                      label_meta_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(event_id),
                        int(h_s),
                        str(sym),
                        0.0,
                        float(ret),
                        float(impact_z),
                        int(now_ms),
                        float(vol),
                        str(regime),
                        json.dumps(label_meta, separators=(",", ":"), sort_keys=True),
                    ),
                )

        con.commit()
    finally:
        con.close()
