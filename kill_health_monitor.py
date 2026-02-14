# kill_health_monitor.py
"""
Auto-kill global execution on data health failure (fail-closed).
"""

import os
import time
import json
from typing import Any, Dict

from engine.dev_core.storage import connect, init_db
from engine.dev_core.kill_switch import activate
from engine.dev_core.health import get_health_snapshot


def _now_ms() -> int:
    return int(time.time() * 1000)


def main() -> int:
    init_db()
    con = connect()
    try:
        health = get_health_snapshot()
        if not health.get("ok", False):
            try:
                activate(
                    "global",
                    "global",
                    reason="auto_data_health_failure",
                    actor="system",
                    meta={"health": health},
                    action="AUTO",
                    con=con,
                )
            except Exception:
                pass

        out: Dict[str, Any] = {
            "ok": True,
            "health_ok": bool(health.get("ok", False)),
            "health": health,
            "ts_ms": _now_ms(),
        }
        print(json.dumps(out, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
