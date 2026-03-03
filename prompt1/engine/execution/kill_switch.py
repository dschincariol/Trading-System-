import json
import os
import time
from typing import Any, Dict, Optional, Tuple

from engine.storage import connect
from engine.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot

SCOPES = {"global", "symbol", "regime"}

ENV_GLOBAL_KEYS = ("KILL_SWITCH_GLOBAL", "TRADING_KILL_SWITCH", "KILL_SWITCH")
ENV_SYMBOLS_KEY = "KILL_SWITCH_SYMBOLS"   # CSV: "SPY,BTC,ETH"
ENV_REGIMES_KEY = "KILL_SWITCH_REGIMES"   # CSV: "low_vol,high_vol,trend,shock"

# ------            -- ------------------------------------------------------
# Circuit breakers (production safety)
# ------            -- ------------------------------------------------------
REQUIRE_FRESH_DATA = os.environ.get("KILL_SWITCH_REQUIRE_FRESH_DATA", "1") == "1"
REQUIRE_FRESH_JOBS = os.environ.get("KILL_SWITCH_REQUIRE_FRESH_JOBS", "1") == "1"

MAX_PRICE_STALE_S = int(os.environ.get("KILL_SWITCH_MAX_PRICE_STALE_S", "300"))          # 5m
MAX_EVENT_STALE_S = int(os.environ.get("KILL_SWITCH_MAX_EVENT_STALE_S", "3600"))         # 1h
MAX_PRED_STALE_S  = int(os.environ.get("KILL_SWITCH_MAX_PRED_STALE_S", "900"))           # 15m

# Heartbeat freshness (job_heartbeats.ts_ms)
MAX_JOB_STALE_S   = int(os.environ.get("KILL_SWITCH_MAX_JOB_STALE_S", "600"))            # 10m
REQUIRED_JOBS_CSV = os.environ.get("KILL_SWITCH_REQUIRED_JOBS", "poll_prices,process_events").strip()

def _now_ms() -> int:
    return int(time.time() * 1000)

def _maybe_auto_expire(con, scope: str, key: str, st):
    """
    Auto-clear DB kill switch if meta_json contains until_ts_ms
    and the time has passed.
    """
    if not st:
        return st

    try:
        enabled = int(st[0] or 0)
    except Exception:
        return st

    if enabled != 1:
        return st

    meta_json = st[3]
    if not meta_json:
        return st

    try:
        meta = json.loads(meta_json)
        until_ms = int(meta.get("until_ts_ms") or 0)
    except Exception:
        return st

    if until_ms <= 0:
        return st

    if _now_ms() <= until_ms:
        return st

    # expired → clear
    try:
                clear(
            scope,
            key,
            reason="auto_expire",
            actor="system",
            meta={"until_ts_ms": int(until_ms)},
            con=con,
        )

    except Exception:
        pass

    try:
        return _read_state(con, scope, key)
    except Exception:
        return None

def _s(x: Any) -> str:
    return str(x or "").strip()

def _norm_scope(scope: str) -> str:
    s = _s(scope).lower()
    if s not in SCOPES:
        raise ValueError(f"invalid kill switch scope: {scope}")
    return s

def _norm_key(scope: str, key: str) -> str:
    k = _s(key)
    if not k:
        raise ValueError("kill switch key required")
    if scope == "global":
        return "global"
    return k

def _env_truthy(v: Optional[str]) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")

def _env_global_enabled() -> bool:
    for k in ENV_GLOBAL_KEYS:
        if _env_truthy(os.environ.get(k)):
            return True
    return False

def _parse_csv_env(name: str) -> Dict[str, bool]:
    raw = _s(os.environ.get(name))
    if not raw:
        return {}
    out: Dict[str, bool] = {}
    for part in raw.split(","):
        p = _s(part)
        if p:
            out[p] = True
    return out

def _env_symbol_enabled(symbol: str) -> bool:
    sym = _s(symbol)
    if not sym:
        return False
    return _parse_csv_env(ENV_SYMBOLS_KEY).get(sym, False)

def _env_regime_enabled(regime: str) -> bool:
    r = _s(regime)
    if not r:
        return False
    m = _parse_csv_env(ENV_REGIMES_KEY)
    if r in m:
        return True
    rl = r.lower()
    for k in m.keys():
        if k.lower() == rl:
            return True
    return False

def _read_state(con, scope: str, key: str) -> Optional[Tuple[int, str, str, str, int, int]]:
    row = con.execute(
        """
        SELECT enabled, reason, actor, meta_json, created_ts_ms, updated_ts_ms
        FROM kill_switch_state
        WHERE scope=? AND key=?
        """,
        (scope, key),
    ).fetchone()
    if not row:
        return None
    try:
        enabled = int(row[0] or 0)
    except Exception:
        enabled = 0
    return (
        enabled,
        _s(row[1]),
        _s(row[2]),
        _s(row[3]),
        int(row[4] or 0),
        int(row[5] or 0),
    )

def set_kill_switch(
    scope: str,
    key: str,
    enabled: int,
    reason: Optional[str] = None,
    actor: str = "system",
    meta: Optional[Dict[str, Any]] = None,
    action: str = "SET",
    con=None,
) -> None:
    scope_n = _norm_scope(scope)
    key_n = _norm_key(scope_n, key)
    en = 1 if int(enabled) else 0
    now_ms = _now_ms()
    meta_json = json.dumps(meta or {}, separators=(",", ":"), sort_keys=True)
    actor_s = _s(actor) or "system"
    reason_s = _s(reason)

    owns = False
    if con is None:
        from engine.storage import init_db
        init_db()
        con = connect()
        owns = True

    try:
        con.execute("BEGIN IMMEDIATE;")

        cur = con.execute(
            """
            SELECT enabled, created_ts_ms
            FROM kill_switch_state
            WHERE scope=? AND key=?
            """,
            (scope_n, key_n),
        ).fetchone()

        if cur is None:
            con.execute(
                """
                INSERT INTO kill_switch_state
                  (scope, key, enabled, reason, actor, meta_json, created_ts_ms, updated_ts_ms)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (scope_n, key_n, en, reason_s, actor_s, meta_json, now_ms, now_ms),
            )
        else:
            created_ms = int(cur[1] or now_ms)

            con.execute(
                """
                UPDATE kill_switch_state
                SET enabled=?, reason=?, actor=?, meta_json=?, updated_ts_ms=?
                WHERE scope=? AND key=?
                """,
                (en, reason_s, actor_s, meta_json, now_ms, scope_n, key_n),
                )
            if created_ms <= 0:
                con.execute(
                    "UPDATE kill_switch_state SET created_ts_ms=? WHERE scope=? AND key=?",
                    (now_ms, scope_n, key_n),
                )

            con.execute(
                """
                INSERT INTO kill_switch_audit
                  (ts_ms, action, scope, key, enabled, actor, reason, meta_json)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    int(now_ms),
                    _s(action).upper() or "SET",
                    scope_n,
                    key_n,
                    int(en),
                    actor_s,
                    reason_s,
                    meta_json,
                ),
            )


        con.execute("COMMIT;")
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        if owns:
            con.close()

def activate(scope: str, key: str, reason: str, actor: str = "system", meta: Optional[Dict[str, Any]] = None, action: str = "AUTO", con=None) -> None:
    set_kill_switch(scope, key, 1, reason=reason, actor=actor, meta=meta, action=action, con=con)

def clear(scope: str, key: str, reason: Optional[str] = None, actor: str = "system", meta: Optional[Dict[str, Any]] = None, con=None) -> None:
    set_kill_switch(scope, key, 0, reason=reason, actor=actor, meta=meta, action="CLEAR", con=con)

def _latest_ts_ms(con, table: str, ts_col: str = "ts_ms") -> int:
    try:
        row = con.execute(f"SELECT MAX({ts_col}) FROM {table}").fetchone()
        return int(row[0] or 0) if row and row[0] is not None else 0
    except Exception:
        return 0

def _job_heartbeat_ts_ms(con, job_name: str) -> int:
    try:
        row = con.execute(
            "SELECT ts_ms FROM job_heartbeats WHERE job_name=?",
            (str(job_name),),
        ).fetchone()
        return int(row[0] or 0) if row and row[0] is not None else 0
    except Exception:
        return 0

def _required_jobs() -> list[str]:
    raw = _s(REQUIRED_JOBS_CSV)
    if not raw:
        return []
    out = []
    for p in raw.split(","):
        s = _s(p)
        if s:
            out.append(s)
    return out

def execution_allowed(
    con=None,
    symbol: Optional[str] = None,
    regime: Optional[str] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    sym = _s(symbol)
    reg = _s(regime)

    # ENV overrides (fail-closed)
    if _env_global_enabled():
        return False, "kill_switch_env_global", {"scope": "global", "key": "global"}

    if sym and _env_symbol_enabled(sym):
        return False, "kill_switch_env_symbol", {"scope": "symbol", "key": sym}

    if reg and _env_regime_enabled(reg):
        return False, "kill_switch_env_regime", {"scope": "regime", "key": reg}

    owns = False
    if con is None:
        con = connect()
        owns = True

    try:
        try:
            from engine.storage import init_db
            init_db()
        except Exception:
            pass

        # Capital guard
        try:
            from engine.capital_guard import trading_allowed as _capital_trading_allowed
            if not _capital_trading_allowed(con=con):
                return False, "capital_guard_block", {"scope": "global", "key": "global"}
        except Exception:
            return False, "capital_guard_error", {"scope": "global", "key": "global"}

        # Data freshness
        if REQUIRE_FRESH_DATA:
            now = _now_ms()

            p_ts = _latest_ts_ms(con, "prices")
            if p_ts <= 0 or (now - p_ts) > (MAX_PRICE_STALE_S * 1000):
                return False, "stale_prices", {"scope": "global", "key": "global"}

            e_ts = _latest_ts_ms(con, "events")
            if e_ts <= 0 or (now - e_ts) > (MAX_EVENT_STALE_S * 1000):
                return False, "stale_events", {"scope": "global", "key": "global"}

            pr_ts = _latest_ts_ms(con, "predictions")
            if pr_ts <= 0 or (now - pr_ts) > (MAX_PRED_STALE_S * 1000):
                return False, "stale_predictions", {"scope": "global", "key": "global"}

        # Job freshness
        if REQUIRE_FRESH_JOBS:
            now = _now_ms()
            for j in _required_jobs():
                hb = _job_heartbeat_ts_ms(con, j)
                if hb <= 0 or (now - hb) > (MAX_JOB_STALE_S * 1000):
                    return False, "stale_job_heartbeat", {"scope": "global", "key": j}

        # DB switches
        st = _read_state(con, "global", "global")
        st = _maybe_auto_expire(con, "global", "global", st)
        if st and int(st[0]) == 1:
            return False, "kill_switch_db_global", {"scope": "global", "key": "global", "reason": st[1], "actor": st[2]}

        if reg:
            st = _read_state(con, "regime", reg)
            st = _maybe_auto_expire(con, "regime", reg, st)
            if st and int(st[0]) == 1:
                return False, "kill_switch_db_regime", {"scope": "regime", "key": reg, "reason": st[1], "actor": st[2]}

        if sym:
            st = _read_state(con, "symbol", sym)
            st = _maybe_auto_expire(con, "symbol", sym, st)
            if st and int(st[0]) == 1:
                return False, "kill_switch_db_symbol", {"scope": "symbol", "key": sym, "reason": st[1], "actor": st[2]}

        return True, "ok", {"scope": None, "key": None}
    finally:
        if owns:
            con.close()

def snapshot(con=None) -> Dict[str, Any]:
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        rows = con.execute(
            """
            SELECT scope, key, enabled, reason, actor, meta_json, created_ts_ms, updated_ts_ms
            FROM kill_switch_state
            ORDER BY scope, key
            """
        ).fetchall()
        out = []
        for r in rows or []:
            out.append(
                {
                    "scope": _s(r[0]),
                    "key": _s(r[1]),
                    "enabled": int(r[2] or 0),
                    "reason": _s(r[3]),
                    "actor": _s(r[4]),
                    "meta": json.loads(r[5] or "{}") if r[5] else {},
                    "created_ts_ms": int(r[6] or 0),
                    "updated_ts_ms": int(r[7] or 0),
                }
            )
        return {"state": out}
    finally:
        if owns:
            con.close()
