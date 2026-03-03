# dev_core/asset_map.py
"""
Asset-class mapping (A.4).
Used for training and inference fallback.

Default mapping is minimal and safe.
Override via env:
  ASSET_CLASS_MAP_JSON='{"SPY":"EQUITY","BTC":"CRYPTO","OIL":"COMMODITY"}'
"""

import json
import os

_DEFAULT = {
    "SPY": "EQUITY",
    "BTC": "CRYPTO",
    "OIL": "COMMODITY",
}

def _load_override():
    raw = os.environ.get("ASSET_CLASS_MAP_JSON", "").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return {str(k).upper(): str(v).upper() for k, v in d.items()}
    except Exception:
        pass
    return {}

_OVERRIDE = _load_override()

def asset_class_for_symbol(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    if not s:
        return "UNKNOWN"
    if s in _OVERRIDE:
        return _OVERRIDE[s]
    if s in _DEFAULT:
        return _DEFAULT[s]

    # lightweight heuristics (safe defaults)
    if s in ("QQQ", "DIA", "IWM", "VTI", "VOO"):
        return "EQUITY"
    if s in ("ETH", "SOL", "BNB", "XRP"):
        return "CRYPTO"
    if s in ("GC", "GOLD", "SI", "SILVER", "CL", "OIL", "NG"):
        return "COMMODITY"
    if s in ("DXY", "EURUSD", "USDJPY", "GBPUSD"):
        return "FX"
    if s in ("TLT", "IEF", "ZB", "ZN"):
        return "RATES"

    return "UNKNOWN"
