# dev_core/drift_utils.py
"""
Helpers around model_drift table.
"""

from engine.storage import connect
from engine.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot


def get_max_drift_ratio(con=None) -> float:
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        row = con.execute("SELECT MAX(drift_ratio) FROM model_drift").fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
    except Exception:
        return 0.0
    finally:
        if owns:
            con.close()


def get_symbol_max_drift_ratio(con, symbol: str) -> float:
    try:
        row = con.execute(
            "SELECT MAX(drift_ratio) FROM model_drift WHERE symbol=?",
            (str(symbol),),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0
    except Exception:
        return 0.0
