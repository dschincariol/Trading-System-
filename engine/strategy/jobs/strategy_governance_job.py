# FILE: strategy_governance_job.py

"""
Unified Strategy Governance Job (FULLY UNIFIED + SAFE)

Preserves:
- strategy_registry stage promotion/demotion (original logic)
- ROI validation thresholds (_roi_pass)
- Metric freshness enforcement per strategy
- Promotion cooldown (PROMOTE_COOLDOWN_S)
- Promotion idempotency guard
- Champion / Challenger tracking
- portfolio_meta persistence
- Optional promotion audit hook
- dev_core.storage job lock (acquire / touch / release)
- Transaction-level locking (BEGIN IMMEDIATE)
- Validation metadata persistence

Adds:
- Capital efficiency gates
- return_per_risk_unit gate
- drawdown_contribution gate
- window_days selection for strategy_metrics
"""

import json
import os
import time
from typing import Dict, Optional, Tuple, List

from engine.runtime.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
)

try:
    from engine.promotion_audit import audit
except Exception:
    audit = None

try:
    from engine.portfolio import init_portfolio_db
except Exception:
    init_portfolio_db = None


# ----------------------------------------------------------------------
# ENV CONTROLS
# ----------------------------------------------------------------------

PROMOTE_STREAK = int(os.environ.get("STRAT_PROMOTE_STREAK", "3"))

MIN_SHARPE = float(os.environ.get("STRAT_MIN_SHARPE", "0.5"))
MAX_DD = float(os.environ.get("STRAT_MAX_DD", "0.25"))

MIN_EFFICIENCY = float(os.environ.get("STRAT_MIN_EFFICIENCY", "0.0"))
MIN_RETURN_PER_RISK = float(os.environ.get("STRAT_MIN_RETURN_PER_RISK", "0.0"))
MAX_DD_CONTRIB = float(os.environ.get("STRAT_MAX_DD_CONTRIB", "-1.0"))

GOV_MIN_NET_CALMAR = float(os.environ.get("GOV_MIN_NET_CALMAR", "0.15"))
GOV_MIN_SHARPE = float(os.environ.get("GOV_MIN_SHARPE", "0.10"))
GOV_MAX_DRAWDOWN = float(os.environ.get("GOV_MAX_DRAWDOWN", "0.35"))
GOV_MIN_TOTAL_RETURN = float(os.environ.get("GOV_MIN_TOTAL_RETURN", "0.00"))

PROMOTE_COOLDOWN_S = int(os.environ.get("PROMOTE_COOLDOWN_S", "3600"))

METRICS_FRESH_S = int(os.environ.get("GOV_METRICS_FRESH_S", "21600"))
METRICS_WINDOW_DAYS = int(os.environ.get("GOV_METRICS_WINDOW_DAYS", "0"))

LOCK_NAME = os.environ.get("GOV_LOCK_NAME", "strategy_governance_job")
LOCK_STALE_S = int(os.environ.get("GOV_LOCK_STALE_S", "600"))
LOCK_WAIT_MS = int(os.environ.get("GOV_LOCK_WAIT_MS", "2500"))

OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()


# ----------------------------------------------------------------------

def _set_busy_timeout(con, ms: int) -> None:
    try:
        con.execute("PRAGMA busy_timeout = ?", (int(max(0, ms)),))
    except Exception:
        try:
            con.execute(f"PRAGMA busy_timeout = {int(max(0, ms))}")
        except Exception:
            pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_json(s: str) -> Dict:
    try:
        x = json.loads(s or "{}")
        return x if isinstance(x, dict) else {}
    except Exception:
        return {}


# ----------------------------------------------------------------------
# ROI VALIDATION (ORIGINAL + EXTENDED)
# ----------------------------------------------------------------------

def _roi_pass(metrics: Dict) -> Tuple[bool, Dict]:
    net_calmar = float(metrics.get("net_calmar", 0.0))
    sharpe = float(metrics.get("sharpe_simple", 0.0))
    max_dd = float(metrics.get("max_drawdown", 0.0))
    total_ret = float(metrics.get("total_return", 0.0))
    efficiency = float(metrics.get("efficiency_score", 0.0))
    ret_per_risk = float(metrics.get("return_per_risk_unit", 0.0))
    dd_contrib = float(metrics.get("drawdown_contribution", 0.0))

    ok = True
    reasons = {
        "net_calmar": net_calmar,
        "sharpe_simple": sharpe,
        "max_drawdown": max_dd,
        "total_return": total_ret,
        "efficiency_score": efficiency,
        "return_per_risk_unit": ret_per_risk,
        "drawdown_contribution": dd_contrib,
    }

    if net_calmar < GOV_MIN_NET_CALMAR:
        ok = False
        reasons["fail_net_calmar"] = True
    if sharpe < GOV_MIN_SHARPE:
        ok = False
        reasons["fail_sharpe"] = True
    if max_dd > GOV_MAX_DRAWDOWN:
        ok = False
        reasons["fail_max_drawdown"] = True
    if total_ret < GOV_MIN_TOTAL_RETURN:
        ok = False
        reasons["fail_total_return"] = True
    if efficiency < MIN_EFFICIENCY:
        ok = False
        reasons["fail_efficiency"] = True
    if ret_per_risk < MIN_RETURN_PER_RISK:
        ok = False
        reasons["fail_return_per_risk"] = True
    if dd_contrib < MAX_DD_CONTRIB:
        ok = False
        reasons["fail_drawdown_contribution"] = True

    return bool(ok), reasons


# ----------------------------------------------------------------------
# portfolio_meta helpers
# ----------------------------------------------------------------------

def _get_meta(con, key: str) -> Optional[str]:
    try:
        row = con.execute("SELECT value FROM portfolio_meta WHERE key=?", (str(key),)).fetchone()
        return str(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def _set_meta(con, key: str, value: str) -> None:
    con.execute(
        """
        INSERT INTO portfolio_meta(key,value)
        VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(key), str(value)),
    )


# ----------------------------------------------------------------------
# Metric freshness enforcement
# ----------------------------------------------------------------------

def _latest_metrics_per_strategy(con, cutoff_ts_ms: int) -> Dict[str, Dict]:
    rows = con.execute(
        """
        SELECT m.strategy_name, m.ts_ms, m.metrics_json
        FROM strategy_metrics m
        JOIN (
          SELECT strategy_name, MAX(ts_ms) AS ts_ms
          FROM strategy_metrics
          WHERE ts_ms>=? AND window_days=?
          GROUP BY strategy_name
        ) t
        ON t.strategy_name=m.strategy_name AND t.ts_ms=m.ts_ms
        """,
        (int(cutoff_ts_ms), int(METRICS_WINDOW_DAYS)),
    ).fetchall()

    out: Dict[str, Dict] = {}
    for r in rows or []:
        out[str(r[0])] = {
            "ts_ms": int(r[1] or 0),
            "metrics": _safe_json(r[2] or "{}"),
        }
    return out


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

def main():
    con = connect()
    try:
        init_db()
        _set_busy_timeout(con, int(LOCK_WAIT_MS))

        if init_portfolio_db:
            try:
                init_portfolio_db()
            except Exception:
                pass

        try:
            con.execute("BEGIN IMMEDIATE;")
        except Exception:
            pass

        if not acquire_job_lock(str(LOCK_NAME), str(OWNER), int(PID), ttl_s=int(LOCK_STALE_S)):
            print(json.dumps({"ok": True, "skipped": True, "reason": "lock held"}))
            return 0

        touch_job_lock(str(LOCK_NAME), str(OWNER), int(PID))

        now_ms = _now_ms()

        # Cooldown
        last_prom = _get_meta(con, "last_strategy_promotion_ts_ms")
        if last_prom and (now_ms - int(last_prom)) < int(PROMOTE_COOLDOWN_S) * 1000:
            release_job_lock(str(LOCK_NAME), str(OWNER), int(PID))
            print(json.dumps({"ok": True, "skipped": True, "reason": "promotion cooldown"}))
            return 0

        cutoff = now_ms - (METRICS_FRESH_S * 1000)
        latest = _latest_metrics_per_strategy(con, cutoff)

        if not latest:
            release_job_lock(str(LOCK_NAME), str(OWNER), int(PID))
            print(json.dumps({"ok": False, "error": "no fresh strategy_metrics"}))
            return 2

        passing: List[Tuple[str, float]] = []
        validation: Dict[str, Dict] = {}

        for name, rec in latest.items():
            m = rec["metrics"]
            ts_ms = rec["ts_ms"]

            # ORIGINAL REGISTRY LOGIC
            sharpe_simple = float(m.get("sharpe_simple", 0.0))
            max_dd = float(m.get("max_drawdown", 1.0))

            reg = con.execute(
                "SELECT stage FROM strategy_registry WHERE strategy_name=?",
                (str(name),),
            ).fetchone()

            if reg:
                if sharpe_simple >= MIN_SHARPE and max_dd <= MAX_DD:
                    con.execute(
                        "UPDATE strategy_registry SET stage='live', updated_ts_ms=? WHERE strategy_name=?",
                        (int(now_ms), str(name)),
                    )
                else:
                    con.execute(
                        "UPDATE strategy_registry SET stage='paper', updated_ts_ms=? WHERE strategy_name=?",
                        (int(now_ms), str(name)),
                    )

            ok, reasons = _roi_pass(m)

            validation[name] = {
                "ok": ok,
                "ts_ms": ts_ms,
                "reasons": reasons,
            }

            try:
                con.execute(
                    "UPDATE strategy_metrics SET is_active=? WHERE strategy_name=? AND ts_ms=?",
                    (1 if ok else 0, str(name), int(ts_ms)),
                )
            except Exception:
                pass

            if not ok:
                continue

            net_calmar = float(m.get("net_calmar", 0.0))
            efficiency = float(m.get("efficiency_score", 0.0))

            score = net_calmar + (sharpe_simple * 0.25) + (efficiency * 0.25) - (max_dd * 0.25)
            passing.append((name, score))

        if not passing:
            _set_meta(con, "last_strategy_validation", json.dumps(validation))
            release_job_lock(str(LOCK_NAME), str(OWNER), int(PID))
            con.commit()
            print(json.dumps({"ok": True, "promoted": False}))
            return 0

        passing.sort(key=lambda x: x[1], reverse=True)

        champion = passing[0][0]
        challenger = passing[1][0] if len(passing) > 1 else None

        current_champ = _get_meta(con, "strategy_champion")
        streak_key = f"strategy_streak::{champion}"
        streak = int(_get_meta(con, streak_key) or 0) + 1
        _set_meta(con, streak_key, str(streak))

        promoted = False

        if streak >= PROMOTE_STREAK:
            if current_champ != champion:
                _set_meta(con, "strategy_champion", champion)
                if challenger:
                    _set_meta(con, "strategy_challenger", challenger)
                _set_meta(con, "last_strategy_promotion_ts_ms", str(now_ms))
                promoted = True

                if audit:
                    try:
                        audit(
                            actor="strategy_governance",
                            action="PROMOTE_STRATEGY",
                            model_name=str(champion),
                            reason={"validation": validation},
                        )
                    except Exception:
                        pass

        _set_meta(con, "last_strategy_validation", json.dumps(validation))
        _set_meta(con, "last_strategy_governance_ts_ms", str(now_ms))

        release_job_lock(str(LOCK_NAME), str(OWNER), int(PID))
        con.commit()

        print(json.dumps({
            "ok": True,
            "champion": champion,
            "challenger": challenger,
            "promoted": promoted,
            "streak": streak,
        }))
        return 0

    except Exception as e:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        try:
            release_job_lock(str(LOCK_NAME), str(OWNER), int(PID))
        except Exception:
            pass
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2

    finally:
        try:
            con.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
