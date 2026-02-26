# analyze_execution_roi.py
"""
Validate that execution conditioning improves realized PnL per risk unit.
"""

import numpy as np
from engine.runtime.storage import connect, init_db

init_db()
con = connect()

rows = con.execute("""
    SELECT pnl_bps, slippage_bps, json_extract(meta,'$.exec_stress.stress_size_mult')
    FROM execution_labels
    WHERE pnl_bps IS NOT NULL
""").fetchall()

if not rows:
    print("No execution_labels found.")
    exit()

pnl = np.array([r[0] for r in rows], dtype=float)
slip = np.array([r[1] for r in rows], dtype=float)
size_mult = np.array([r[2] if r[2] is not None else 1.0 for r in rows], dtype=float)

print("Mean pnl_bps:", pnl.mean())
print("Mean slippage_bps:", slip.mean())
print("Corr(size_mult, pnl):", np.corrcoef(size_mult, pnl)[0,1])
