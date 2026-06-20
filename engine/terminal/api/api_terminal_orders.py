# engine/api/api_terminal_orders.py
"""
Terminal Order Entry (risk-gated; paper-safe by default)

Routes:
  POST /api/terminal/order
  POST /api/terminal/flatten
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict

from engine.runtime.storage import connect
from engine.runtime.gates import execution_gate_snapshot


ROUTE_SPECS_TERMINAL_ORDERS = [
    ("POST", "/api/terminal/order",   "api_post_terminal_order"),
    ("POST", "/api/terminal/flatten", "api_post_terminal_flatten"),
]


def _json_body(handler) -> Dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _table_exists(con, name: str) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (name,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def api_post_terminal_order(handler, _ctx=None):
    body = _json_body(handler)

    symbol = str(body.get("symbol") or "").strip().upper()
    side = str(body.get("side") or "").strip().upper()
    qty = float(body.get("qty") or 0)

    if not symbol or qty <= 0 or side not in ("BUY", "SELL"):
        return {"ok": False, "error": "invalid_order"}

    gate = execution_gate_snapshot()
    if not gate.get("allowed", False):
        return {"ok": False, "error": "execution_blocked", "gate": gate}

    con = connect(readonly=False)

    if not _table_exists(con, "portfolio_orders"):
        return {"ok": False, "error": "portfolio_orders_missing"}

    ts = int(time.time() * 1000)

    try:
        con.execute(
            """
            INSERT INTO portfolio_orders (ts_ms, symbol, action, from_side, to_side,
                                          from_weight, to_weight, delta_weight, source_alert_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, symbol, side, None, side, 0.0, 0.0, qty, None),
        )
        con.commit()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, "symbol": symbol, "side": side, "qty": qty}


def api_post_terminal_flatten(handler, _ctx=None):
    body = _json_body(handler)
    symbol = str(body.get("symbol") or "").strip().upper()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}

    gate = execution_gate_snapshot()
    if not gate.get("allowed", False):
        return {"ok": False, "error": "execution_blocked", "gate": gate}

    con = connect(readonly=False)

    if not _table_exists(con, "broker_positions"):
        return {"ok": False, "error": "broker_positions_missing"}
    if not _table_exists(con, "portfolio_orders"):
        return {"ok": False, "error": "portfolio_orders_missing"}

    try:
        row = con.execute(
            "SELECT qty FROM broker_positions WHERE symbol=? LIMIT 1",
            (symbol,),
        ).fetchone()

        if not row:
            return {"ok": True, "message": "no_position"}

        qty = float(row[0] or 0.0)
        if qty == 0:
            return {"ok": True, "message": "already_flat"}

        side = "SELL" if qty > 0 else "BUY"
        ts = int(time.time() * 1000)

        con.execute(
            """
            INSERT INTO portfolio_orders (ts_ms, symbol, action, from_side, to_side,
                                          from_weight, to_weight, delta_weight, source_alert_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, symbol, "FLATTEN", None, side, 0.0, 0.0, abs(qty), None),
        )
        con.commit()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    return {"ok": True, "symbol": symbol, "flatten_qty": abs(qty)}