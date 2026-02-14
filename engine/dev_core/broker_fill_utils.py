# dev_core/broker_fill_utils.py
"""
Utilities to aggregate broker fills into realized entry/exit prices.
"""

from typing import Optional, Dict, Any, Tuple
from engine.dev_core.storage import connect
from engine.dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot


def _vwap(rows):
    qty_sum = 0.0
    notional = 0.0
    for px, qty in rows:
        q = abs(float(qty))
        qty_sum += q
        notional += q * float(px)
    if qty_sum <= 0:
        return None
    return notional / qty_sum


def get_realized_trade(
    *,
    symbol: str,
    entry_ts_ms: int,
    exit_ts_ms: int,
) -> Optional[Dict[str, Any]]:
    """
    Returns realized execution info using broker_fills.
    We assume:
      - entry fills between [entry_ts_ms, entry_ts_ms + small buffer]
      - exit fills between [exit_ts_ms - buffer, exit_ts_ms + buffer]
    """

    BUFFER_MS = 60_000  # 60s tolerance

    con = connect()
    try:
        # Fail-soft if labels table isn't created yet
        try:
            con.execute("SELECT 1 FROM labels LIMIT 1").fetchone()
        except Exception:
            return 0

        rows = con.execute(
            """
            SELECT price, qty, side, ts_ms, fees
            FROM broker_fills
            WHERE symbol=?
              AND ts_ms BETWEEN ? AND ?
            ORDER BY ts_ms ASC
            """,
            (
                symbol,
                int(entry_ts_ms - BUFFER_MS),
                int(exit_ts_ms + BUFFER_MS),
            ),
        ).fetchall()

        if not rows:
            return None

        entry_rows = []
        exit_rows = []
        fees_total = 0.0
        side = None

        for price, qty, s, ts, fees in rows:
            fees_total += float(fees or 0.0)
            if side is None:
                side = s

            # heuristic split: early fills = entry, later fills = exit
            if ts <= entry_ts_ms + BUFFER_MS:
                entry_rows.append((price, qty))
            elif ts >= exit_ts_ms - BUFFER_MS:
                exit_rows.append((price, qty))

        if not entry_rows:
            return None

        px_in = _vwap(entry_rows)
        px_out = _vwap(exit_rows) if exit_rows else None

        if px_in is None:
            return None

        return {
            "side": 1 if str(side).upper().startswith("B") else -1,
            "px_in": float(px_in),
            "px_out": float(px_out) if px_out is not None else None,
            "fees_total": float(fees_total),
        }

    finally:
        con.close()
