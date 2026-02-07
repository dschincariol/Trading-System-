# CREATE NEW FILE: dev_core/weather_region_map_validate.py
"""
Validates data/weather_region_map.json for required structure.
Exit code 0 OK, 2 invalid.
"""

import os
import json
import sys


def _fail(msg: str) -> None:
    print(f"[weather_region_map_validate] ERROR: {msg}")
    raise SystemExit(2)


def main():
    path = os.environ.get("WEATHER_REGION_MAP", os.path.join("data", "weather_region_map.json"))
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception as e:
        _fail(f"cannot load {path}: {e!r}")

    regions = (cfg or {}).get("regions")
    if not isinstance(regions, dict) or not regions:
        _fail("missing or empty `regions` dict")

    for rid, r in regions.items():
        if not rid:
            _fail("empty region id")
        if not isinstance(r, dict):
            _fail(f"region {rid} must be object")
        if "lat" not in r or "lon" not in r:
            _fail(f"region {rid} must include lat/lon")
        try:
            float(r["lat"]); float(r["lon"])
        except Exception:
            _fail(f"region {rid} lat/lon must be numeric")

    symbols = (cfg or {}).get("symbols") or {}
    if not isinstance(symbols, dict):
        _fail("`symbols` must be dict if present")

    for sym, v in symbols.items():
        if not sym:
            _fail("empty symbol key in symbols map")
        if isinstance(v, str):
            if v not in regions:
                _fail(f"symbol {sym} refers to unknown region {v}")
        elif isinstance(v, list):
            for it in v:
                if isinstance(it, str):
                    if it not in regions:
                        _fail(f"symbol {sym} refers to unknown region {it}")
                elif isinstance(it, dict):
                    rid = it.get("region_id")
                    if not rid or rid not in regions:
                        _fail(f"symbol {sym} refers to unknown region {rid}")
                    try:
                        float(it.get("weight", 1.0))
                    except Exception:
                        _fail(f"symbol {sym} weight must be numeric")
                else:
                    _fail(f"symbol {sym} mapping entries must be str or object")
        else:
            _fail(f"symbol {sym} mapping must be str or list")

    print("[weather_region_map_validate] OK")
    return 0


if __name__ == "__main__":
    main()
