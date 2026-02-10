# dev_core/equity_snapshot.py
import time
from dev_core.storage import connect, init_db


def snapshot_equity(ts_ms: int = None) -> bool:
    init_db()
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)

    con = connect()
    try:
        r = con.execute("SELECT equity FROM broker_account LIMIT 1").fetchone()
        if not r:
            return False
        eq = float(r[0] or 0.0)
        

        con.execute(
            "INSERT OR REPLACE INTO equity_history(ts_ms, equity) VALUES (?,?)",
            (int(ts_ms), float(eq)),
        )
        con.commit()
        return True
    except Exception:
        return False
    finally:
        con.close()
