# engine/api/http_parsing.py
# Shared HTTP helpers used by multiple API modules.
from __future__ import annotations

from typing import Any, Dict, Optional
from urllib.parse import parse_qs


def qs(parsed: Any) -> Dict[str, str]:
    """
    Parse querystring into a simple dict[str,str].
    Accepts:
      - parsed=None -> {}
      - parsed.query (string) -> parse
      - parsed as dict -> returns shallow stringified values
    """
    if parsed is None:
        return {}

    if isinstance(parsed, dict):
        out: Dict[str, str] = {}
        for k, v in parsed.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                out[str(k)] = "" if not v else str(v[0])
            else:
                out[str(k)] = str(v)
        return out

    q = getattr(parsed, "query", None)
    if not q:
        return {}

    raw = parse_qs(str(q), keep_blank_values=True)
    out2: Dict[str, str] = {}
    for k, v in raw.items():
        out2[str(k)] = "" if not v else str(v[0])
    return out2


def deny_if_shutdown() -> Optional[Dict[str, Any]]:
    """
    Returns an error dict if lifecycle is in shutdown, else None.
    """
    try:
        from engine.runtime.lifecycle import lifecycle_snapshot
        snap = lifecycle_snapshot()
        if snap.get("state") == "SHUTDOWN":
            return {"ok": False, "error": "shutdown_in_progress"}
    except Exception:
        pass
    return None