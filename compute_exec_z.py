# compute_exec_z.py
"""
Compute net_z and gross_z for labels_exec by symbol+horizon using rolling stats.
"""

import time
import math
from collections import defaultdict

from dev_core.storage import connect, init_db


def main():
    init_db()
    con = connect()
    try:

        rows = con.execute(
            """
            SELECT event_id, symbol, horizon_s, net_ret, gross_ret
            FROM labels_exec
            WHERE net_ret IS NOT NULL AND gross_ret IS NOT NULL
            """
        ).fetchall()

        by_key = defaultdict(list)
        for eid, sym, h, net_r, gross_r in rows:
            try:
                by_key[(str(sym), int(h))].append((int(eid), float(net_r), float(gross_r)))
            except Exception:
                continue

        updates = []
        for (sym, h), items in by_key.items():
            # robust std on net_ret
            net_vals = [x[1] for x in items]
            gross_vals = [x[2] for x in items]
            if len(net_vals) < 30:
                continue

            def std(v):
                m = sum(v) / len(v)
                var = sum((x - m) ** 2 for x in v) / max(1, (len(v) - 1))
                return math.sqrt(var)

            net_std = std(net_vals)
            gross_std = std(gross_vals)
            if net_std <= 1e-12:
                continue

            for eid, net_r, gross_r in items:
                net_z = net_r / net_std
                gross_z = (gross_r / gross_std) if gross_std > 1e-12 else None
                updates.append((gross_z, net_z, int(eid), sym, int(h)))

        for gross_z, net_z, eid, sym, h in updates:
            con.execute(
                """
                UPDATE labels_exec
                SET gross_z=?, net_z=?
                WHERE event_id=? AND symbol=? AND horizon_s=?
                """,
                (gross_z, float(net_z), int(eid), str(sym), int(h)),
            )

        con.commit()
        print(f"[labels_exec] updated z for {len(updates)} rows")
    finally:
        con.close()


if __name__ == "__main__":
    main()
