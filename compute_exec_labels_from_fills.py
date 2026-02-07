# compute_exec_labels_from_fills.py
"""
Phase 5.1: Override execution labels using real broker fills.
"""

import json
import time
from dev_core.storage import connect, init_db
from dev_core.broker_fill_utils import get_realized_trade


def _now_ms():
    return int(time.time() * 1000)


def main():
    init_db()
    con = connect()
    try:
        # Fail-soft if labels table isn't created yet
        try:
            con.execute("SELECT 1 FROM labels LIMIT 1").fetchone()
        except Exception:
            return 0

        rows = con.execute(
            """
            SELECT p.event_id, p.symbol, p.horizon_s, p.ts_ms
            FROM predictions p
            JOIN broker_orders bo
              ON bo.symbol=p.symbol AND bo.ts_ms >= p.ts_ms
            LEFT JOIN labels_exec le
              ON le.event_id=p.event_id AND le.symbol=p.symbol AND le.horizon_s=p.horizon_s
            WHERE le.realized=0 OR le.realized IS NULL
            ORDER BY p.ts_ms ASC
            LIMIT 10000
            """
        ).fetchall()

        n_used = 0
        n_skip = 0

        for eid, sym, horizon_s, ts_ms in rows:
            exit_ts = ts_ms + int(horizon_s) * 1000

            trade = get_realized_trade(
                symbol=str(sym),
                entry_ts_ms=int(ts_ms),
                exit_ts_ms=int(exit_ts),
            )

            if not trade:
                n_skip += 1
                continue

            side = trade["side"]
            px_in = trade["px_in"]
            px_out = trade["px_out"]

            # If no exit fills yet, mark-to-market later
            if px_out is None:
                continue

            gross_ret = (px_out / px_in - 1.0) * float(side)

            # Fees already realized
            net_ret = gross_ret - trade["fees_total"]

            pass

        con.execute(
                """
                INSERT OR REPLACE INTO labels_exec(
                  event_id, symbol, horizon_s, ts_ms,
                  side, gross_ret, net_ret,
                  gross_z, net_z,
                  mid_in, mid_out, spread_in,
                  fees_bps, slippage_bps, spread_bps, total_cost_bps,
                  source, realized, extra_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(eid),
                    str(sym),
                    int(horizon_s),
                    int(ts_ms),
                    int(side),
                    float(gross_ret),
                    float(net_ret),
                    None,
                    None,
                    float(px_in),
                    float(px_out),
                    None,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    "broker_fills",
                    1,
                    json.dumps(trade),
                ),
            )
            n_used += 1

        con.commit()
        print(f"[labels_exec:fills] used={n_used} skipped={n_skip}")

    finally:
        con.close()


if __name__ == "__main__":
    main()
