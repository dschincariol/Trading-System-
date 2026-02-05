# jobs/shadow_metrics_job.py
import json
import time
import math
from dev_core.storage import connect

WINDOW_MS = 6 * 60 * 60 * 1000  # 6h

def _now_ms():
    return int(time.time() * 1000)

def run():
    con = connect()
    try:
        end_ms = _now_ms()
        start_ms = end_ms - WINDOW_MS

        rows = con.execute(
            """
            SELECT p.symbol, p.regime, p.horizon_s, p.model_name,
                   p.predicted_z, p.net_pred_z, l.realized_z
            FROM shadow_predictions p
            JOIN labels l
              ON l.event_id = p.event_id
             AND l.symbol = p.symbol
             AND l.horizon_s = p.horizon_s
            WHERE p.ts_ms BETWEEN ? AND ?
            """,
            (start_ms, end_ms),
        ).fetchall()

        by_key = {}
        for sym, reg, h, m, pz, npz, rz in rows:
            k = (reg, h, m)
            by_key.setdefault(k, []).append((float(pz), float(npz) if npz is not None else None, float(rz)))

        for (reg, h, m), vals in by_key.items():
            n = len(vals)
            if n < 5:
                continue

            se = 0.0
            ne = 0.0
            ae = 0.0
            da = 0
            cntn = 0

            for pz, npz, rz in vals:
                e = pz - rz
                se += e * e
                ae += abs(e)
                if (pz >= 0) == (rz >= 0):
                    da += 1
                if npz is not None:
                    ne += (npz - rz) ** 2
                    cntn += 1

            rmse = math.sqrt(se / n)
            mae = ae / n
            dir_acc = da / n
            net_rmse = math.sqrt(ne / cntn) if cntn else None

            con.execute(
                """
                INSERT INTO shadow_metrics
                  (window_start_ms, window_end_ms, regime, model_name,
                   horizon_s, rmse, mae, dir_acc, avg_cost, net_rmse, n, extra_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    start_ms,
                    end_ms,
                    reg,
                    m,
                    h,
                    rmse,
                    mae,
                    dir_acc,
                    None,
                    net_rmse,
                    n,
                    json.dumps({}, separators=(",", ":"), sort_keys=True),
                ),
            )

        con.commit()
    finally:
        con.close()

if __name__ == "__main__":
    run()
