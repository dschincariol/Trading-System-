# dev_core/alpha_lifecycle_engine.py
"""
Alpha Lifecycle Engine (ALE)

Tracks alpha lifecycle tied to alerts (signals) WITHOUT changing signal generation.

Responsibilities:
- Register alpha instances (alert_id keyed) on-demand
- Enforce TTL expiry and compute alpha_remaining via half-life decay
- Provide explainable lifecycle state for EPE audits
"""

import json
import math
import time
from typing import Any, Dict, Optional


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_tables(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS alpha_lifecycle (
          alert_id INTEGER PRIMARY KEY,
          created_ts_ms INTEGER NOT NULL,
          expires_ts_ms INTEGER NOT NULL,
          half_life_ms INTEGER NOT NULL,
          volatility REAL,
          status TEXT NOT NULL,
          last_touch_ts_ms INTEGER NOT NULL,
          meta_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_alpha_lifecycle_exp ON alpha_lifecycle(expires_ts_ms);
        """
    )


def register_alpha(
    con,
    alert_id: int,
    created_ts_ms: int,
    ttl_ms: int,
    half_life_ms: int,
    volatility: float = None,
    meta: Dict[str, Any] = None,
) -> None:
    _ensure_tables(con)

    now = _now_ms()
    created = int(created_ts_ms or 0) if int(created_ts_ms or 0) > 0 else int(now)
    ttl = int(ttl_ms or 0)
    ttl = max(1, ttl)
    half_life = int(half_life_ms or 0)
    half_life = max(1, half_life)

    expires = int(created) + int(ttl)
    meta_json = json.dumps(meta or {}, separators=(",", ":"), sort_keys=True)

    con.execute(
        """
        INSERT INTO alpha_lifecycle(
          alert_id, created_ts_ms, expires_ts_ms, half_life_ms, volatility, status, last_touch_ts_ms, meta_json
        )
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(alert_id) DO UPDATE SET
          created_ts_ms=excluded.created_ts_ms,
          expires_ts_ms=excluded.expires_ts_ms,
          half_life_ms=excluded.half_life_ms,
          volatility=excluded.volatility,
          status=excluded.status,
          last_touch_ts_ms=excluded.last_touch_ts_ms,
          meta_json=excluded.meta_json
        """,
        (
            int(alert_id),
            int(created),
            int(expires),
            int(half_life),
            (float(volatility) if volatility is not None else None),
            "ACTIVE",
            int(now),
            meta_json,
        ),
    )


def ensure_alpha_from_intent(con, intent: Dict[str, Any]) -> None:
    """
    Idempotently register alpha lifecycle for intents that include source_alert_id + signal_ts_ms + ttl/half-life.
    """
    try:
        a_id = intent.get("source_alert_id")
        if a_id is None:
            return
        created = int(intent.get("signal_ts_ms") or 0)
        ttl = int(intent.get("alpha_ttl_ms") or 0)
        hl = int(intent.get("alpha_half_life_ms") or 0)
        vol = intent.get("volatility")
        register_alpha(
            con,
            alert_id=int(a_id),
            created_ts_ms=int(created),
            ttl_ms=int(ttl),
            half_life_ms=int(hl),
            volatility=(float(vol) if vol is not None else None),
            meta={"symbol": intent.get("symbol"), "reason": intent.get("reason")},
        )
    except Exception:
        return


def alpha_state(con, alert_id: int, now_ms: int = None) -> Dict[str, Any]:
    _ensure_tables(con)
    now = int(now_ms) if now_ms is not None else _now_ms()

    r = con.execute(
        """
        SELECT created_ts_ms, expires_ts_ms, half_life_ms, volatility, status, meta_json
        FROM alpha_lifecycle
        WHERE alert_id=?
        """,
        (int(alert_id),),
    ).fetchone()

    if not r:
        return {"ok": False, "exists": False}

    created, exp, hl, vol, status, meta_json = r
    created = int(created or 0)
    exp = int(exp or 0)
    hl = int(hl or 1)

    age = max(0, now - created)
    ttl = max(1, exp - created)
    expired = now >= exp

    if expired and str(status or "").upper() != "EXPIRED":
        try:
            con.execute(
                """
                UPDATE alpha_lifecycle
                SET status='EXPIRED', last_touch_ts_ms=?
                WHERE alert_id=?
                """,
                (int(now), int(alert_id)),
            )
        except Exception:
            pass

    # alpha_remaining: half-life decay, clipped by TTL wall
    rem = 0.0
    if not expired:
        try:
            rem_hl = math.pow(0.5, float(age) / float(max(1, hl)))
            ttl_frac = max(0.0, 1.0 - (float(age) / float(max(1, ttl))))
            rem = max(0.0, min(1.0, rem_hl * (0.5 + 0.5 * ttl_frac)))
        except Exception:
            rem = 0.0

    meta = None
    try:
        meta = json.loads(meta_json) if meta_json else None
    except Exception:
        meta = None

    return {
        "ok": True,
        "exists": True,
        "created_ts_ms": created,
        "expires_ts_ms": exp,
        "half_life_ms": int(hl),
        "volatility": (float(vol) if vol is not None else None),
        "status": ("EXPIRED" if expired else "ACTIVE"),
        "age_ms": int(age),
        "ttl_ms": int(ttl),
        "alpha_remaining": float(rem),
        "meta": (meta if isinstance(meta, dict) else None),
    }
