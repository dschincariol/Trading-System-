# engine/runtime/allocator_status.py

import json
import os
import time
from typing import Any, Dict, Optional, Tuple

from engine.runtime.storage import connect


FRESH_MAX_AGE_S = int(os.environ.get("ALLOCATOR_FRESH_MAX_AGE_S", "1800"))  # 30m


def _now_ms() -> int:
    return int(time.time() * 1000)


def _table_exists(con, name: str) -> bool:
    try:
        r = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(name),),
        ).fetchone()
        return bool(r)
    except Exception:
        return False


def _latest_row(con, table: str, *, where: str = "", args: Tuple[Any, ...] = ()) -> Optional[tuple]:
    try:
        q = f"SELECT * FROM {table} {where} ORDER BY ts_ms DESC LIMIT 1"
        return con.execute(q, args).fetchone()
    except Exception:
        return None


def _count(con, table: str, *, where: str = "", args: Tuple[Any, ...] = ()) -> int:
    try:
        q = f"SELECT COUNT(1) FROM {table} {where}"
        r = con.execute(q, args).fetchone()
        return int(r[0] or 0) if r else 0
    except Exception:
        return 0


def get_allocator_status(*, window_days: int = 0) -> Dict[str, Any]:
    ts_ms = _now_ms()
    wd = int(window_days)

    con = connect()
    try:
        out: Dict[str, Any] = {
            "ok": False,
            "ts_ms": int(ts_ms),
            "window_days": int(wd),
            "fresh_max_age_s": int(FRESH_MAX_AGE_S),
            "allocator": {},
            "sleeves": {},
            "strategies": {},
            "reasons": [],
        }

        # tables presence
        has_sleeve_alloc = _table_exists(con, "sleeve_allocations")
        has_strat_alloc = _table_exists(con, "strategy_allocations")
        has_sleeve_metrics = _table_exists(con, "sleeve_metrics")
        has_strat_metrics = _table_exists(con, "strategy_metrics")

        out["tables"] = {
            "sleeve_allocations": bool(has_sleeve_alloc),
            "strategy_allocations": bool(has_strat_alloc),
            "sleeve_metrics": bool(has_sleeve_metrics),
            "strategy_metrics": bool(has_strat_metrics),
        }

        # latest sleeve allocations
        if has_sleeve_alloc:
            r = _latest_row(con, "sleeve_allocations", where="WHERE window_days=?", args=(int(wd),))
            if r:
                # schema: ts_ms, window_days, allocations_json, reason_json
                out["sleeves"]["ts_ms"] = int(r[0] or 0)
                try:
                    out["sleeves"]["weights"] = json.loads(r[2] or "{}")
                except Exception:
                    out["sleeves"]["weights"] = {}
                try:
                    out["sleeves"]["reason"] = json.loads(r[3] or "{}") if r[3] else {}
                except Exception:
                    out["sleeves"]["reason"] = {}
            else:
                out["reasons"].append("missing_sleeve_allocations_row")

        # latest strategy allocations
        if has_strat_alloc:
            r = _latest_row(con, "strategy_allocations", where="WHERE window_days=?", args=(int(wd),))
            if r:
                out["strategies"]["ts_ms"] = int(r[0] or 0)
                try:
                    out["strategies"]["weights"] = json.loads(r[2] or "{}")
                except Exception:
                    out["strategies"]["weights"] = {}
                try:
                    out["strategies"]["reason"] = json.loads(r[3] or "{}") if r[3] else {}
                except Exception:
                    out["strategies"]["reason"] = {}
            else:
                out["reasons"].append("missing_strategy_allocations_row")

        # counts (debug)
        if has_sleeve_metrics:
            out["sleeves"]["n_metrics"] = _count(con, "sleeve_metrics", where="WHERE window_days=?", args=(int(wd),))
        if has_strat_metrics:
            out["strategies"]["n_metrics"] = _count(con, "strategy_metrics", where="WHERE window_days=?", args=(int(wd),))

        # freshness gate
        freshest = 0
        try:
            freshest = max(int(out.get("sleeves", {}).get("ts_ms") or 0), int(out.get("strategies", {}).get("ts_ms") or 0))
        except Exception:
            freshest = 0

        age_s = (int(ts_ms) - int(freshest)) / 1000.0 if freshest > 0 else 1e18
        out["allocator"]["latest_ts_ms"] = int(freshest)
        out["allocator"]["age_s"] = float(age_s)

        if freshest <= 0:
            out["reasons"].append("allocator_never_ran")
            out["ok"] = False
        elif age_s > float(FRESH_MAX_AGE_S):
            out["reasons"].append("allocator_stale")
            out["ok"] = False
        else:
            out["ok"] = True

        return out
    finally:
        try:
            con.close()
        except Exception:
            pass