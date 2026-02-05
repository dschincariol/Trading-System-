"""
Poll broker fills + compute slippage metrics + pnl attribution snapshots.

Run every 60s (or 300s) in production.

Env:
  EXEC_POLL_LOOKBACK_S=3600
"""

import os
import time
import json
import logging

from dev_core.execution_ledger import (
    init_execution_ledger,
    compute_metrics_snapshot,
    compute_pnl_attribution_snapshot,
)
from dev_core.storage import connect

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [execution_poll_and_attrib] %(message)s",
)

POLL_LOOKBACK_S = int(os.environ.get("EXEC_POLL_LOOKBACK_S", "3600"))


def main() -> int:
    init_execution_ledger()

    after_ts_ms = int(time.time() * 1000) - int(POLL_LOOKBACK_S) * 1000

    # Poll fills from each broker adapter that exists.
    try:
        from dev_core.broker_alpaca_rest import poll_and_log_fills
        poll_and_log_fills(after_ts_ms=after_ts_ms)
    except Exception:
        pass

    try:
        from dev_core.broker_ibkr_gateway import poll_and_log_fills as ibkr_poll
        ibkr_poll(after_ts_ms=after_ts_ms)
    except Exception:
        pass

    # Compute slippage + m2m metrics snapshot
    m = compute_metrics_snapshot(limit_orders=5000)

    # Compute pnl attribution snapshot (by signal/source_alert_id)
    a = compute_pnl_attribution_snapshot(lookback_orders=5000)

    out = {"ok": True, "metrics": m, "attribution": a, "ts_ms": int(time.time() * 1000)}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
