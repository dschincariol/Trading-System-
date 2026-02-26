# dev_core/drawdown_state.py
from typing import Optional, Tuple
from engine.runtime.storage import connect, init_db


def get_current_drawdown(con=None) -> float:
    """
    Compute drawdown from equity_history: dd = 1 - equity/peak.
    Returns 0.0 if history missing.
    """
    init_db()
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        rows = con.execute(
            "SELECT equity FROM equity_history ORDER BY ts_ms ASC"
        ).fetchall()
        if not rows or len(rows) < 5:
            return 0.0

        peak = 0.0
        cur = 0.0
        for (eq,) in rows:
            try:
                e = float(eq or 0.0)
            except Exception:
                continue
            if e > peak:
                peak = e
            cur = e

        if peak <= 0:
            return 0.0
        dd = 1.0 - (cur / peak)
        if dd < 0.0:
            dd = 0.0
        if dd > 1.0:
            dd = 1.0
        return float(dd)
    finally:
        if owns:
            con.close()

# ============================================================
# DRAWdown velocity (for TSE tail-risk trigger)
# ============================================================

def get_drawdown_velocity(con):
    try:
        rows = con.execute(
            """
            SELECT drawdown
            FROM equity_snapshots
            ORDER BY ts_ms DESC
            LIMIT 5
            """
        ).fetchall()
        if not rows or len(rows) < 2:
            return 0.0
        latest = float(rows[0][0] or 0.0)
        prev = float(rows[1][0] or 0.0)
        return abs(latest - prev)
    except Exception:
        return 0.0
