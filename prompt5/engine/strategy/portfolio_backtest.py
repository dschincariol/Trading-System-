# portfolio_backtest.py
"""
Portfolio backtest harness wired to labels table.

Method:
- Walk alerts chronologically
- At each alert timestamp, build a target portfolio from recent alerts
- Score portfolio step return proxy using labels: realized impact_z aligned to alert horizon

Outputs (SQLite):
- portfolio_bt_runs
- portfolio_bt_points
"""

import json
import os
import time
import math
import statistics
import logging

from engine.storage import connect, init_db
from engine.regime_stack import compute_regime_vector, regime_compatibility, regime_model_version
from engine.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from engine.execution_policy_engine import apply_execution_policy
from engine.portfolio import (
    init_portfolio_db,
    PORTFOLIO_LOOKBACK_S,
    PORTFOLIO_MIN_CONF,
    PORTFOLIO_MIN_ABS_Z,
    PORTFOLIO_MAX_POSITIONS,
    PORTFOLIO_GROSS_CAP,
    PORTFOLIO_MAX_W_PER_SYMBOL,
)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [portfolio_backtest] %(message)s",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_bt_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  start_ts_ms INTEGER NOT NULL,
  end_ts_ms INTEGER NOT NULL,
  metrics_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_bt_points (
  run_id INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL,
  ret REAL NOT NULL,
  equity REAL NOT NULL,
  drawdown REAL NOT NULL,
  exec_cost REAL DEFAULT 0.0,
  slippage REAL DEFAULT 0.0,
  fees REAL DEFAULT 0.0,
  detail_json TEXT,
  PRIMARY KEY (run_id, ts_ms)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_bt_points_run ON portfolio_bt_points(run_id, ts_ms);
CREATE INDEX IF NOT EXISTS idx_portfolio_bt_points_ts  ON portfolio_bt_points(ts_ms);

CREATE TABLE IF NOT EXISTS strategy_metrics (
  strategy_name TEXT NOT NULL,
  window_days INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL,
  start_ts_ms INTEGER NOT NULL,
  end_ts_ms INTEGER NOT NULL,
  metrics_json TEXT NOT NULL,
  PRIMARY KEY (strategy_name, window_days)
);

CREATE TABLE IF NOT EXISTS rl_shadow_eval (
  policy_name TEXT NOT NULL,
  run_id INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL,
  baseline_metrics_json TEXT NOT NULL,
  policy_metrics_json TEXT NOT NULL,
  delta_metrics_json TEXT NOT NULL,
  PRIMARY KEY (policy_name, run_id)
);

CREATE TABLE IF NOT EXISTS rl_shadow_actions (
  run_id INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL,
  step_idx INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  baseline_action_json TEXT NOT NULL,
  rl_action_json TEXT NOT NULL,
  step_ret REAL NOT NULL,
  equity REAL NOT NULL,
  drawdown REAL NOT NULL,
  turnover REAL NOT NULL,
  reward REAL NOT NULL,
  PRIMARY KEY (run_id, ts_ms)
);

CREATE TABLE IF NOT EXISTS rl_policies (
  policy_name TEXT PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  params_json TEXT NOT NULL,
  metrics_json TEXT NOT NULL
);
"""

# -------------            -- ------------------------------------------------------
# Small numeric guards
# -------------            -- ------------------------------------------------------

def _cost_bps_from_trade(trade: dict, px_in: float, px_out: float, side: int) -> dict:
    """
    Step 6: Best-effort execution cost decomposition in bps.
    Returns dict with:
      fees_bps, slippage_bps, spread_bps, total_cost_bps, spread_in
    All fields are floats (>=0 where applicable).
    """
    try:
        pin = float(px_in)
        pout = float(px_out)
        sgn = float(side) if int(side) != 0 else 1.0
    except Exception:
        return {"fees_bps": 0.0, "slippage_bps": 0.0, "spread_bps": 0.0, "total_cost_bps": 0.0, "spread_in": None}

    if pin <= 1e-12:
        return {"fees_bps": 0.0, "slippage_bps": 0.0, "spread_bps": 0.0, "total_cost_bps": 0.0, "spread_in": None}

    # Fees: accept a few possible keys
    fees_total = 0.0
    try:
        fees_total = float(trade.get("fees_total") or trade.get("fees") or 0.0)
    except Exception:
        fees_total = 0.0

    # Convert fees into bps relative to entry notional (qty is unknown here, so treat px as 1-share notional)
    # If you later add qty, swap this to fees / (abs(qty)*pin).
    fees_bps = 0.0
    try:
        fees_bps = float(fees_total) / float(pin) * 10000.0
        if fees_bps != fees_bps or fees_bps < 0:
            fees_bps = 0.0
    except Exception:
        fees_bps = 0.0

    # Slippage: if trade provides a ref price, compare fill to ref in sign-aware bps.
    slippage_bps = 0.0
    ref_px = None
    try:
        ref_px = trade.get("ref_px")
        if ref_px is None:
            ref_px = trade.get("mid_in")
        if ref_px is not None:
            ref_px = float(ref_px)
    except Exception:
        ref_px = None

    if ref_px is not None and ref_px > 1e-12:
        try:
            # buy worse if fill > ref; sell worse if fill < ref -> sign by side
            slippage_bps = ((float(pin) - float(ref_px)) / float(ref_px)) * 10000.0 * float(sgn)
            # cost should be positive "worse"; flip sign if needed
            slippage_bps = -float(slippage_bps)
            if slippage_bps != slippage_bps:
                slippage_bps = 0.0
        except Exception:
            slippage_bps = 0.0

    # Spread: if trade provides spread_in or bid/ask, compute.
    spread_in = None
    spread_bps = 0.0
    try:
        si = trade.get("spread_in")
        if si is None:
            bid = trade.get("bid_in")
            ask = trade.get("ask_in")
            if bid is not None and ask is not None:
                si = float(ask) - float(bid)
        if si is not None:
            spread_in = float(si)
    except Exception:
        spread_in = None

    if spread_in is not None and pin > 1e-12:
        try:
            spread_bps = float(spread_in) / float(pin) * 10000.0
            if spread_bps != spread_bps or spread_bps < 0:
                spread_bps = 0.0
        except Exception:
            spread_bps = 0.0

    total_cost_bps = float(max(0.0, fees_bps)) + float(max(0.0, slippage_bps)) + float(max(0.0, spread_bps))

    return {
        "fees_bps": float(max(0.0, fees_bps)),
        "slippage_bps": float(max(0.0, slippage_bps)),
        "spread_bps": float(max(0.0, spread_bps)),
        "total_cost_bps": float(max(0.0, total_cost_bps)),
        "spread_in": (float(spread_in) if spread_in is not None else None),
    }


def _now_ms():
    return int(time.time() * 1000)

def _safe_f(x, d=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else d
    except Exception:
        return d

# RL env (must be after _safe_f)
RL_TRAIN = os.environ.get("RL_TRAIN", "0") == "1"
RL_POLICY_NAME = os.environ.get("RL_POLICY_NAME", "conf_threshold_v1")
RL_GRID_MIN_CONF = _safe_f(os.environ.get("RL_GRID_MIN_CONF", "0.50"), 0.50)
RL_GRID_MAX_CONF = _safe_f(os.environ.get("RL_GRID_MAX_CONF", "0.90"), 0.90)
RL_GRID_STEP_CONF = _safe_f(os.environ.get("RL_GRID_STEP_CONF", "0.02"), 0.02)
RL_LAMBDA_DD = _safe_f(os.environ.get("RL_LAMBDA_DD", "0.5"), 0.5)
RL_LAMBDA_TURN = _safe_f(os.environ.get("RL_LAMBDA_TURN", "0.1"), 0.1)

def _safe_mean(xs):
    xs = list(xs or [])
    return float(statistics.mean(xs)) if xs else 0.0

def _safe_stdev(xs):
    xs = list(xs or [])
    if len(xs) < 2:
        return 0.0
    try:
        return float(statistics.stdev(xs))
    except Exception:
        return 0.0

def _safe_i(x, d=0):
    try:
        return int(x)
    except Exception:
        return int(d)

def _is_finite(x):
    try:
        return math.isfinite(float(x))
    except Exception:
        return False

def _clamp(x, lo, hi):
    return max(float(lo), min(float(hi), float(x)))

def _slice_curve_window(curve, end_ts_ms, window_days):
    if not curve or window_days <= 0:
        return curve
    cutoff = int(end_ts_ms) - int(window_days) * 86400 * 1000
    return [p for p in curve if int(p[0]) >= cutoff]

def _risk_metrics_from_curve(curve, total_return, max_drawdown):
    rets = [float(p[1]) for p in (curve or [])]
    n = len(rets)
    mu = _safe_mean(rets)
    vol = _safe_stdev(rets)
    sharpe = (mu / vol) * math.sqrt(n) if vol > 1e-12 and n > 1 else 0.0
    downside = [r for r in rets if r < 0.0]
    dvol = _safe_stdev(downside)
    sortino = (mu / dvol) * math.sqrt(n) if dvol > 1e-12 and n > 1 else 0.0
    calmar = (total_return / abs(max_drawdown)) if abs(max_drawdown) > 1e-12 else 0.0
    return {
        "ret_mean": mu,
        "ret_volatility": vol,
        "downside_volatility": dvol,
        "sharpe_simple": sharpe,
        "sortino_simple": sortino,
        "calmar_simple": calmar,
        "n_returns": n,
    }

# -------------            -- ------------------------------------------------------
# DB init
# -------------            -- ------------------------------------------------------

def _ensure_tables(con):
    con.executescript(SCHEMA)
    con.commit()

def _get_tse_state(con):
    try:
        row = con.execute(
            "SELECT ts_ms, state, fp_streak, slippage_z, latency_var_z FROM trade_suppression_state WHERE id=1"
        ).fetchone()
        if not row:
            return None
        return {
            "ts_ms": int(row[0]),
            "state": str(row[1]),
            "fp_streak": int(row[2]) if row[2] is not None else None,
            "slippage_z": float(row[3]) if row[3] is not None else None,
            "latency_var_z": float(row[4]) if row[4] is not None else None,
        }
    except Exception:
        return None

# -------------            -- ------------------------------------------------------
# Label lookup
# -------------            -- ------------------------------------------------------

def _resolve_event_id(con, alert_ts, title):
    t = (title or "").strip()
    if not t:
        return None

    row = con.execute(
        "SELECT id FROM events WHERE title=? AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1",
        (t, int(alert_ts)),
    ).fetchone()
    if row:
        return int(row[0])

    row = con.execute(
        "SELECT id FROM events WHERE title=? ORDER BY ABS(ts_ms-?) ASC LIMIT 1",
        (t, int(alert_ts)),
    ).fetchone()
    return int(row[0]) if row else None

def _realized_label(con, event_id, symbol, horizon_s):
    r = con.execute(
        """
        SELECT impact_z
        FROM labels
        WHERE event_id=? AND symbol=? AND horizon_s=? AND impact_z IS NOT NULL
        """,
        (int(event_id), str(symbol), int(horizon_s)),
    ).fetchone()
    if not r:
        return None
    try:
        v = float(r[0])
        return v if math.isfinite(v) else None
    except Exception:
        return None

# -------------            -- ------------------------------------------------------
# Portfolio construction from alerts (baseline)
# -------------            -- ------------------------------------------------------

def _targets_from_recent_alerts(con, now_ms, lookback_s):
    cutoff = int(now_ms) - int(lookback_s) * 1000

    # detect optional columns
    try:
        cols = [r[1] for r in (con.execute("PRAGMA table_info(alerts)").fetchall() or [])]
    except Exception:
        cols = []
    has_severity = "severity" in cols
    has_title = "event_title" in cols
    has_event_id = "event_id" in cols

    if has_event_id and has_severity and has_title:
        rows = con.execute(
            """
            SELECT id, ts_ms, symbol, horizon_s, expected_z, confidence, severity, event_title, event_id
            FROM alerts
            WHERE ts_ms >= ? AND ts_ms < ?
            ORDER BY ts_ms DESC
            """,
            (int(cutoff), int(now_ms)),
        ).fetchall()
    elif has_severity and has_title:
        rows = con.execute(
            """
            SELECT id, ts_ms, symbol, horizon_s, expected_z, confidence, severity, event_title
            FROM alerts
            WHERE ts_ms >= ? AND ts_ms < ?
            ORDER BY ts_ms DESC
            """,
            (int(cutoff), int(now_ms)),
        ).fetchall()
    elif has_severity:
        rows = con.execute(
            """
            SELECT id, ts_ms, symbol, horizon_s, expected_z, confidence, severity
            FROM alerts
            WHERE ts_ms >= ? AND ts_ms < ?
            ORDER BY ts_ms DESC
            """,
            (int(cutoff), int(now_ms)),
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT id, ts_ms, symbol, horizon_s, expected_z, confidence
            FROM alerts
            WHERE ts_ms >= ? AND ts_ms < ?
            ORDER BY ts_ms DESC
            """,
            (int(cutoff), int(now_ms)),
        ).fetchall()

    best = {}
    for row in (rows or []):
        aid = row[0]
        ts = row[1]
        sym = str(row[2] or "").strip()
        h = row[3]
        z = _safe_f(row[4], 0.0)
        conf = _safe_f(row[5], 0.0)

        if not sym:
            continue
        if conf < float(PORTFOLIO_MIN_CONF):
            continue
        if abs(z) < float(PORTFOLIO_MIN_ABS_Z):
            continue

        sev = ""
        title = ""
        event_id = None

        if has_severity:
            sev = str(row[6] or "")
        if has_title:
            title = str(row[7] or "")
        if has_event_id:
            event_id = row[8] if len(row) > 8 else None

        sev_u = str(sev or "").upper()
        score = abs(z) * conf
        if sev_u == "CRIT":
            score *= 1.15
        elif sev_u == "HIGH":
            score *= 1.08

        cur = best.get(sym)
        if cur is None or float(score) > float(cur["_score"]):
            if event_id is None and title:
                event_id = _resolve_event_id(con, int(ts), title)

            best[sym] = {
                "symbol": sym,
                "side": "LONG" if z > 0 else "SHORT",
                "weight": float(
                    min(
                        (float(score) / 3.0) * float(PORTFOLIO_GROSS_CAP),
                        float(PORTFOLIO_MAX_W_PER_SYMBOL),
                    )
                ),
                "event_id": (int(event_id) if event_id is not None else None),
                "horizon_s": int(h),
                "expected_z": float(z),
                "confidence": float(conf),
                "severity": str(sev or ""),
                "event_title": str(title or ""),
                # optional execution fields for later patches; default 0
                "exec_cost": 0.0,
                "slippage": 0.0,
                "fees": 0.0,
                "regime_model_version": str(regime_model_version()),
                "regime_vector": compute_regime_vector(symbol=sym, ts_ms=int(now_ms), con=con),
                "regime_compatibility": 1.0,
                "_score": float(score),

            }

    positions = list(best.values())[: int(PORTFOLIO_MAX_POSITIONS)]
    gross = sum(abs(float(p.get("weight", 0.0))) for p in positions)
    if gross > float(PORTFOLIO_GROSS_CAP) and gross > 1e-12:
        s = float(PORTFOLIO_GROSS_CAP) / float(gross)
        for p in positions:
            p["weight"] = float(p["weight"]) * float(s)
    return positions

def _positions_filter_conf(positions, conf_min):
    return [p for p in positions or [] if _safe_f(p.get("confidence"), 0.0) >= conf_min]

def _posmap_signed(positions):
    m = {}
    for p in positions or []:
        sym = p.get("symbol")
        w = _safe_f(p.get("weight"), 0.0)
        side = str(p.get("side") or "").upper()
        sw = w if side == "LONG" else -w
        if sym:
            m[sym] = sw
    return m

def _turnover_between(prev, cur):
    a = _posmap_signed(prev)
    b = _posmap_signed(cur)
    return 0.5 * sum(abs(b.get(s, 0.0) - a.get(s, 0.0)) for s in set(a) | set(b))

def _recompute_step_ret(positions):
    gross = sum(abs(_safe_f(p.get("weight"), 0.0)) for p in positions) or 1.0
    r = 0.0
    for p in positions:
        if p.get("realized_impact_z") is None:
            continue
        w = _safe_f(p.get("weight"), 0.0)
        side = str(p.get("side") or "").upper()
        signed = w if side == "LONG" else -w
        r += (signed / gross) * float(p["realized_impact_z"])
    return r

# -------------            -- ------------------------------------------------------
# Main backtest
# -------------            -- ------------------------------------------------------

def run_backtest():
    con = connect()
    metrics = {}
    run_id = None
    try:
        try:
            init_portfolio_db()
        except Exception:
            pass

        _ensure_tables(con)

        start_capital = _safe_f(os.environ.get("BT_START_EQUITY", "1.0"), 1.0)
        if (not _is_finite(start_capital)) or float(start_capital) <= 0.0:
            start_capital = 1.0

        days = _safe_i(os.environ.get("BT_DAYS", "60"), 60)
        days = max(1, int(days))

        lookback_s = _safe_i(os.environ.get("BT_LOOKBACK_S", PORTFOLIO_LOOKBACK_S), int(PORTFOLIO_LOOKBACK_S))
        lookback_s = max(60, int(lookback_s))

        now_ms = _now_ms()
        start_ms = now_ms - int(days) * 86400 * 1000

        run_id = con.execute(
            """
            INSERT INTO portfolio_bt_runs(ts_ms, start_ts_ms, end_ts_ms, metrics_json)
            VALUES (?,?,?,?)
            """,
            (int(now_ms), int(start_ms), int(now_ms), "{}"),
        ).lastrowid

        equity = float(start_capital)
        peak = float(start_capital)
        max_dd = 0.0
        curve = []  # (ts_ms, ret, equity, drawdown, detail_dict)

        # TSE / suppression accounting (for tail-risk validation)
        suppression_blocks = 0
        suppression_nonblocks = 0
        suppression_state_counts = {}

        alerts = con.execute(
            """
            SELECT ts_ms
            FROM alerts
            WHERE ts_ms >= ?
            ORDER BY ts_ms ASC
            """,
            (int(start_ms),),
        ).fetchall()

        for (ts,) in (alerts or []):
            ts_ms = int(ts)
            positions = _targets_from_recent_alerts(con, ts_ms, lookback_s)

            # --- Route through EPE so TSE can HARD_BLOCK / SOFT_THROTTLE / SIZE_COMPRESSION in backtest
            intents = []
            for p in (positions or []):
                sym = str(p.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                side = str(p.get("side") or "").upper().strip()
                w = float(_safe_f(p.get("weight", 0.0), 0.0))
                if w <= 0.0:
                    continue
                intents.append(
                    {
                        "symbol": sym,
                        "to_side": ("LONG" if side == "LONG" else "SHORT"),
                        "to_weight": float(w),
                        "signal_ts_ms": int(ts_ms),
                        # provide defaults so EPE TTL/half-life works even without alert_id linkage
                        "alpha_ttl_ms": int(os.environ.get("EPE_DEFAULT_TTL_MS", str(5 * 60 * 1000))),
                        "alpha_half_life_ms": int(os.environ.get("EPE_DEFAULT_HALF_LIFE_MS", str(90 * 1000))),
                        "source_alert_id": None,
                        "source_order_id": None,
                    }
                )

            shaped = apply_execution_policy(
                con=con,
                intents=intents,
                actor="backtest",
                mode="backtest",
                broker="sim",
                portfolio_orders_batch_id=None,
                default_signal_ts_ms=int(ts_ms),
            )

            # Read current TSE state snapshot (written by EPE)
            tse_state = _get_tse_state(con)
            tse_key = str((tse_state or {}).get("state") or "NONE")
            suppression_state_counts[tse_key] = int(suppression_state_counts.get(tse_key, 0)) + 1

            if intents and not shaped:
                # HARD_BLOCK (or all intents suppressed) -> treat as no trades for this step
                suppression_blocks += 1
                positions = []
            else:
                suppression_nonblocks += 1
                # Apply any size compression / throttle already embedded in shaped to_weight
                wmap = {}
                smap = {}
                for o in (shaped or []):
                    try:
                        s = str(o.get("symbol") or "").strip().upper()
                        if not s:
                            continue
                        wmap[s] = float(o.get("to_weight") or 0.0)
                        smap[s] = str(o.get("to_side") or "").upper().strip()
                    except Exception:
                        continue

                new_positions = []
                for p in (positions or []):
                    sym = str(p.get("symbol") or "").strip().upper()
                    if not sym:
                        continue
                    if sym not in wmap:
                        continue
                    nw = float(wmap.get(sym) or 0.0)
                    if nw <= 0.0:
                        continue
                    p = dict(p)
                    p["weight"] = float(nw)
                    # keep side consistent
                    if smap.get(sym) in ("LONG", "SHORT"):
                        p["side"] = smap.get(sym)
                    new_positions.append(p)
                positions = new_positions

            gross = sum(abs(float(p.get("weight", 0.0))) for p in positions)
            gross = float(gross if gross > 1e-12 else 1.0)

            step_ret = 0.0
            for p in positions:
                eid = p.get("event_id")
                if eid is None:
                    continue
                impact = _realized_label(con, int(eid), p.get("symbol"), int(p.get("horizon_s", 0)))
                if impact is None:
                    continue
                w = float(_safe_f(p.get("weight"), 0.0))
                side = str(p.get("side") or "").upper()
                signed = w if side == "LONG" else -w
                step_ret += (signed / gross) * float(impact)

            exec_cost = float(sum(_safe_f(p.get("exec_cost", 0.0), 0.0) for p in positions))
            slippage = float(sum(_safe_f(p.get("slippage", 0.0), 0.0) for p in positions))
            fees = float(sum(_safe_f(p.get("fees", 0.0), 0.0) for p in positions))

            equity *= (1.0 + float(step_ret))
            equity -= float(exec_cost)

            if equity > peak:
                peak = float(equity)
            drawdown = (float(equity) / float(peak) - 1.0) if peak > 1e-12 else 0.0
            if drawdown < max_dd:
                max_dd = float(drawdown)

            detail = {
                "positions": positions,
                "lookback_s": int(lookback_s),
                "gross_abs_weight": float(gross),
                "exec_cost": float(exec_cost),
                "slippage": float(slippage),
                "fees": float(fees),

                # TSE snapshot for this step (if present)
                "tse_state": tse_state,
            }

            con.execute(
                """
                INSERT OR REPLACE INTO portfolio_bt_points
                (run_id, ts_ms, ret, equity, drawdown, exec_cost, slippage, fees, detail_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(run_id),
                    int(ts_ms),
                    float(step_ret),
                    float(equity),
                    float(drawdown),
                    float(exec_cost),
                    float(slippage),
                    float(fees),
                    json.dumps(detail),
                ),
            )

            curve.append((int(ts_ms), float(step_ret), float(equity), float(drawdown), detail))

        equity_end = float(equity)
        start_cap = float(start_capital) if float(start_capital) > 1e-12 else 1.0
        total_return = (equity_end / start_cap - 1.0)

        risk = _risk_metrics_from_curve(curve, total_return=float(total_return), max_drawdown=float(max_dd))

        total_exec_cost = con.execute(
            "SELECT SUM(exec_cost) FROM portfolio_bt_points WHERE run_id=?",
            (int(run_id),),
        ).fetchone()[0] or 0.0

        total_slippage = con.execute(
            "SELECT SUM(slippage) FROM portfolio_bt_points WHERE run_id=?",
            (int(run_id),),
        ).fetchone()[0] or 0.0

        total_fees = con.execute(
            "SELECT SUM(fees) FROM portfolio_bt_points WHERE run_id=?",
            (int(run_id),),
        ).fetchone()[0] or 0.0

        metrics = {
            "final_equity": float(equity_end),
            "max_drawdown": float(max_dd),
            "total_return": float(total_return),
            "steps": int(len(curve)),
            **risk,
            "total_exec_cost": float(total_exec_cost),
            "total_slippage": float(total_slippage),
            "total_fees": float(total_fees),

            # TSE / suppression validation metrics
            "suppression_blocks": int(suppression_blocks),
            "suppression_nonblocks": int(suppression_nonblocks),
            "suppression_state_counts": dict(suppression_state_counts),
        }

        con.execute(
            """
            UPDATE portfolio_bt_runs
            SET end_ts_ms=?, metrics_json=?
            WHERE id=?
            """,
            (int(now_ms), json.dumps(metrics), int(run_id)),
        )

        con.commit()
        logging.info(
            "BACKTEST_COMPLETE run_id=%s final_equity=%.4f max_dd=%.4f",
            int(run_id),
            float(metrics.get("final_equity", 0.0)),
            float(metrics.get("max_drawdown", 0.0)),
        )
        return {"ok": True, "run_id": int(run_id), "metrics": metrics}

    finally:
        try:
            con.close()
        except Exception:
            pass

# -------------            -- ------------------------------------------------------
# CLI entry
# -------------            -- ------------------------------------------------------

def main():
    res = run_backtest()
    print(json.dumps(res, indent=2))

if __name__ == "__main__":
    main()
