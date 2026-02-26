import os
import time
from typing import Dict, Any, Optional

from engine.strategy.drawdown_state import get_current_drawdown
from engine.runtime.risk_state import get_state, set_state
from engine.runtime.storage import connect

# thresholds (hard stop)
MAX_DRAWDOWN = float(os.environ.get("CAPITAL_STOP_DRAWDOWN", "0.25"))  # 25%
COOLDOWN_DAYS = int(os.environ.get("CAPITAL_COOLDOWN_DAYS", "5"))

# -----------------------------
# Capital Preservation Mode (CPM)
# -----------------------------
CAPITAL_PRESERVE_DD_VELOCITY = float(os.environ.get("CAPITAL_PRESERVE_DD_VELOCITY", "0.02"))
CAPITAL_PRESERVE_STRESS_SCORE = float(os.environ.get("CAPITAL_PRESERVE_STRESS_SCORE", "0.75"))

# execution degradation triggers (best-effort from execution_analytics)
CAPITAL_PRESERVE_EXEC_COST_BPS = float(os.environ.get("CAPITAL_PRESERVE_EXEC_COST_BPS", "18.0"))
CAPITAL_PRESERVE_EXEC_LAT_MS = float(os.environ.get("CAPITAL_PRESERVE_EXEC_LAT_MS", "1200"))
CAPITAL_PRESERVE_EXEC_LOOKBACK_H = float(os.environ.get("CAPITAL_PRESERVE_EXEC_LOOKBACK_H", "24"))

# exit hysteresis / flip-flop control
CAPITAL_PRESERVE_MIN_DURATION_S = int(os.environ.get("CAPITAL_PRESERVE_MIN_DURATION_S", "1800"))
CAPITAL_PRESERVE_EXIT_STREAK = int(os.environ.get("CAPITAL_PRESERVE_EXIT_STREAK", "3"))


def trading_allowed(con=None) -> bool:
    state = get_state("trading_state", "enabled")
    if state != "enabled":
        return False

    dd = get_current_drawdown(con)
    if dd >= MAX_DRAWDOWN:
        set_state("trading_state", "stopped")
        set_state("stop_reason", f"drawdown={dd:.2%}")
        set_state("stop_ts_ms", str(int(time.time() * 1000)))
        return False

    return True


def maybe_release_cooldown(con=None):
    """
    Re-enable trading after cooldown days AND drawdown improved.
    """
    state = get_state("trading_state", "enabled")
    if state != "stopped":
        return

    ts = int(get_state("stop_ts_ms", "0") or "0")
    if ts <= 0:
        return

    days = (time.time() * 1000 - ts) / (86400 * 1000)
    if days < COOLDOWN_DAYS:
        return

    dd = get_current_drawdown(con)
    if dd < MAX_DRAWDOWN * 0.75:
        set_state("trading_state", "enabled")
        set_state("stop_reason", "")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _drawdown_velocity(con=None) -> float:
    """
    Velocity proxy: Δdrawdown since last call.
    Persist last snapshot in risk_state for stability across processes.

    NOTE: Preserves existing behavior:
      returns (cur_dd - prev_dd) as a raw delta (NOT time-normalized).
    """
    try:
        prev = float(get_state("capital_prev_drawdown", "0") or "0")
    except Exception:
        prev = 0.0
    cur = 0.0
    try:
        cur = float(get_current_drawdown(con) or 0.0)
    except Exception:
        cur = 0.0
    try:
        set_state("capital_prev_drawdown", str(cur))
    except Exception:
        pass
    return float(cur - prev)


def _stress_snapshot(con=None) -> Dict[str, Any]:
    try:
        from engine.market_stress import get_market_stress_snapshot
    except Exception:
        return {"stress_score": 0.0}

    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        return get_market_stress_snapshot(con=con, ts_ms=_now_ms()) or {"stress_score": 0.0}
    except Exception:
        return {"stress_score": 0.0}
    finally:
        if owns:
            try:
                con.close()
            except Exception:
                pass


def _exec_degradation_snapshot(con=None) -> Dict[str, Any]:
    """
    Best-effort query from execution_analytics (if present).
    Returns:
      ok, avg_total_cost_bps, avg_slippage_bps, avg_latency_ms, n

    Preserves existing behavior/shape exactly, but adds safety:
      - fail-soft if table/columns do not exist
    """
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        since = _now_ms() - int(float(CAPITAL_PRESERVE_EXEC_LOOKBACK_H) * 3600.0 * 1000.0)

        # Guard: execution_analytics might not exist yet
        try:
            chk = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='execution_analytics'"
            ).fetchone()
            if not chk:
                return {
                    "ok": False,
                    "n": 0,
                    "avg_total_cost_bps": 0.0,
                    "avg_slippage_bps": 0.0,
                    "avg_latency_ms": 0.0,
                }
        except Exception:
            # if master check fails, continue best-effort
            pass

        try:
            row = con.execute(
                """
                SELECT
                  COUNT(*) AS n,
                  AVG(total_cost_bps) AS avg_cost,
                  AVG(slippage_bps) AS avg_slip,
                  AVG(age_ms) AS avg_age
                FROM execution_analytics
                WHERE ts_ms >= ?
                """,
                (int(since),),
            ).fetchone()
        except Exception:
            row = None

        if not row:
            return {
                "ok": False,
                "n": 0,
                "avg_total_cost_bps": 0.0,
                "avg_slippage_bps": 0.0,
                "avg_latency_ms": 0.0,
            }

        n, avg_cost, avg_slip, avg_age = row
        return {
            "ok": True,
            "n": int(n or 0),
            "avg_total_cost_bps": float(avg_cost or 0.0),
            "avg_slippage_bps": float(avg_slip or 0.0),
            "avg_latency_ms": float(avg_age or 0.0),
        }
    finally:
        if owns:
            try:
                con.close()
            except Exception:
                pass


def update_capital_preservation_mode(con=None) -> Dict[str, Any]:
    """
    Entry/Exit rules for Capital Preservation Mode (CPM).

    Triggers:
      - drawdown velocity spikes
      - execution quality degradation (execution_analytics)
      - market stress elevation

    Effects are applied in:
      - dev_core/portfolio.py (frequency + gross compression)
      - dev_core/position_sizing.py (size compression)
      - dev_core/execution_policy_engine.py (aggressiveness reduction)
    """
    now = _now_ms()

    mode = str(get_state("capital_mode", "normal") or "normal")
    ts0 = int(get_state("capital_mode_ts_ms", "0") or "0")
    reason0 = str(get_state("capital_mode_reason", "") or "")

    dd_vel = _drawdown_velocity(con)
    st = _stress_snapshot(con)
    try:
        stress_score = float(st.get("stress_score", 0.0) or 0.0)
    except Exception:
        stress_score = 0.0

    ex = _exec_degradation_snapshot(con)
    exec_bad = False
    if ex.get("ok") and int(ex.get("n") or 0) > 0:
        try:
            if float(ex.get("avg_total_cost_bps") or 0.0) >= float(CAPITAL_PRESERVE_EXEC_COST_BPS):
                exec_bad = True
        except Exception:
            pass
        try:
            if float(ex.get("avg_latency_ms") or 0.0) >= float(CAPITAL_PRESERVE_EXEC_LAT_MS):
                exec_bad = True
        except Exception:
            pass

    entry = (
        float(dd_vel) >= float(CAPITAL_PRESERVE_DD_VELOCITY)
        or float(stress_score) >= float(CAPITAL_PRESERVE_STRESS_SCORE)
        or bool(exec_bad)
    )

    # -----------------------------
    # ENTER
    # -----------------------------
    if entry and mode != "preserve":
        parts = [
            f"dd_vel={float(dd_vel):.4f}>=th={float(CAPITAL_PRESERVE_DD_VELOCITY):.4f}"
            if float(dd_vel) >= float(CAPITAL_PRESERVE_DD_VELOCITY)
            else f"dd_vel={float(dd_vel):.4f}",
            f"stress={float(stress_score):.3f}>=th={float(CAPITAL_PRESERVE_STRESS_SCORE):.3f}"
            if float(stress_score) >= float(CAPITAL_PRESERVE_STRESS_SCORE)
            else f"stress={float(stress_score):.3f}",
        ]
        if ex.get("ok"):
            parts.append(f"exec_cost_bps={float(ex.get('avg_total_cost_bps') or 0.0):.2f}")
            parts.append(f"exec_lat_ms={float(ex.get('avg_latency_ms') or 0.0):.0f}")
            parts.append(f"exec_slip_bps={float(ex.get('avg_slippage_bps') or 0.0):.2f}")
        if exec_bad:
            parts.append("exec_bad=1")

        set_state("capital_mode", "preserve")
        set_state("capital_mode_ts_ms", str(int(now)))
        set_state("capital_mode_reason", "|".join(parts))
        set_state("capital_mode_exit_streak", "0")

        return {
            "ok": True,
            "capital_mode": "preserve",
            "reason": "|".join(parts),
            "dd_vel": float(dd_vel),
            "stress_score": float(stress_score),
            "exec": ex,
        }

    # -----------------------------
    # EXIT (hysteresis + streak)
    # -----------------------------
    if mode == "preserve":
        # minimum time in preserve
        if ts0 > 0 and (now - ts0) < int(CAPITAL_PRESERVE_MIN_DURATION_S) * 1000:
            return {"ok": True, "capital_mode": "preserve", "reason": reason0, "min_duration_hold": 1}

        # conditions must be "good" to build exit streak
        good = (
            float(dd_vel) < float(CAPITAL_PRESERVE_DD_VELOCITY) * 0.50
            and float(stress_score) < float(CAPITAL_PRESERVE_STRESS_SCORE) * 0.85
            and (not bool(exec_bad))
        )

        streak = int(get_state("capital_mode_exit_streak", "0") or "0")
        if good:
            streak += 1
        else:
            streak = 0
        set_state("capital_mode_exit_streak", str(int(streak)))

        if streak >= int(CAPITAL_PRESERVE_EXIT_STREAK):
            set_state("capital_mode", "normal")
            set_state("capital_mode_reason", "")
            set_state("capital_mode_ts_ms", str(int(now)))
            set_state("capital_mode_exit_streak", "0")
            return {"ok": True, "capital_mode": "normal", "exited": 1}

        return {"ok": True, "capital_mode": "preserve", "reason": reason0, "exit_streak": int(streak)}

    return {"ok": True, "capital_mode": mode or "normal"}
