# NEW FILE: engine/runtime/lifecycle_state.py
# CREATE THIS FILE EXACTLY:

import time
from typing import Dict

from engine.runtime.runtime_meta import meta_get, meta_set

STATE_KEY = "lifecycle_state"

BOOTING = "BOOTING"
SCHEMA_REPAIR = "SCHEMA_REPAIR"
WARMING_UP = "WARMING_UP"
LIVE = "LIVE"
DEGRADED = "DEGRADED"
SHUTTING_DOWN = "SHUTTING_DOWN"


def set_state(state: str, detail: str = "") -> None:
    meta_set(STATE_KEY, str(state))
    if detail is not None:
        meta_set("lifecycle_detail", str(detail))


def get_state() -> Dict:
    return {
        "state": meta_get(STATE_KEY, BOOTING),
        "detail": meta_get("lifecycle_detail", ""),
        "first_price_ts_ms": meta_get("first_price_ts_ms", ""),
        "schema_version": meta_get("schema_version", ""),
        "last_clean_shutdown_ts_ms": meta_get("last_clean_shutdown_ts_ms", ""),
    }


def mark_clean_shutdown() -> None:
    meta_set("last_clean_shutdown_ts_ms", str(int(time.time() * 1000)))
    set_state(SHUTTING_DOWN, "clean_shutdown")