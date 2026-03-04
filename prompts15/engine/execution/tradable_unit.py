import os
from typing import Any, Dict, List, Optional, Tuple


def _s(x: Any) -> str:
    return str(x or "").strip()


def _default_instrument_type(symbol: str) -> str:
    # Minimal default: repo is equity-centric today.
    # Can be overridden per-intent or via env.
    it = _s(os.environ.get("DEFAULT_INSTRUMENT_TYPE", "EQUITY"))
    return it.upper() or "EQUITY"


def _default_venue_for_broker(broker: str) -> str:
    b = _s(broker).lower()
    if b in ("ibkr", "interactivebrokers", "interactive_brokers", "ib_gateway", "ibgateway", "tws"):
        return _s(os.environ.get("DEFAULT_VENUE_IBKR", "IBKR_SMART")).upper() or "IBKR_SMART"
    if b in ("alpaca", "alpaca_rest"):
        return _s(os.environ.get("DEFAULT_VENUE_ALPACA", "ALPACA"))
    if b in ("sim", "paper", "sandbox"):
        return _s(os.environ.get("DEFAULT_VENUE_SIM", "SIM"))
    return _s(os.environ.get("DEFAULT_VENUE", ""))


def normalize_tradable_unit_fields(
    order: Dict[str, Any],
    *,
    broker: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    o = dict(order or {})

    sym = _s(o.get("symbol")).upper()
    it = _s(o.get("instrument_type")).upper()
    venue = _s(o.get("venue")).upper()

    changes: Dict[str, Any] = {}

    if not sym:
        return o, {"ok": False, "reason": "missing_symbol"}

    if not it:
        it = _default_instrument_type(sym)
        o["instrument_type"] = it
        changes["instrument_type"] = it

    if not venue:
        venue = _default_venue_for_broker(_s(broker)) if broker else _s(os.environ.get("DEFAULT_VENUE", ""))
        venue = venue.upper()
        if venue:
            o["venue"] = venue
            changes["venue"] = venue

    return o, {"ok": True, "changes": changes}


def normalize_orders(
    orders: List[Dict[str, Any]],
    *,
    broker: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    audits: List[Dict[str, Any]] = []

    for idx, o in enumerate(list(orders or [])):
        if not isinstance(o, dict):
            continue
        no, meta = normalize_tradable_unit_fields(o, broker=broker)
        out.append(no)
        audits.append({"i": int(idx), "meta": meta})

    return out, {"ok": True, "n": int(len(out)), "audit": audits}
