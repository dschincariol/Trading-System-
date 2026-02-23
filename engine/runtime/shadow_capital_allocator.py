# engine/dev_core/shadow_capital_allocator.py
"""
Shadow Capital Allocation Scoring

Computes model-level risk-adjusted governance scores so safer models can beat
higher-PnL risky ones.

Inputs (best-effort, optional):
- shadow_metrics (rmse/dir_acc/net_rmse + n)
- trade_attribution_ledger (slippage_bps, pnl, fees) with model_json hints
- equity_history / portfolio_bt_points (optional drawdown proxy)

Persists to:
- shadow_capital_scores (created in dev_core/storage.py init_db)
"""

import json
import os
import math
import time
from typing import Dict, Any, List, Optional

from engine.runtime.storage import connect as _db_connect


DEFAULT_WINDOW_S = int(os.environ.get("SHADOW_CAPITAL_WINDOW_S", "86400"))  # 24h
DEFAULT_REGIME = os.environ.get("SHADOW_CAPITAL_REGIME", "global").strip() or "global"

# Composite weights (env overridable)
W_DIR_ACC = float(os.environ.get("SHADOW_W_DIR_ACC", "1.0"))
W_NET_RMSE = float(os.environ.get("SHADOW_W_NET_RMSE", "1.0"))
W_SLIP_MEAN = float(os.environ.get("SHADOW_W_SLIP_MEAN", "1.0"))
W_SLIP_STD = float(os.environ.get("SHADOW_W_SLIP_STD", "0.5"))
W_DD = float(os.environ.get("SHADOW_W_DD", "1.0"))
W_CAP_EFF = float(os.environ.get("SHADOW_W_CAP_EFF", "1.0"))

# Guardrails
MIN_N = int(os.environ.get("SHADOW_CAPITAL_MIN_N", "20"))
MAX_ROWS_ATTR = int(os.environ.get("SHADOW_CAPITAL_MAX_ATTR_ROWS", "200000"))


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


def _safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _safe_int(x, default=0):
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


def _try_json_load(s: Any) -> Dict[str, Any]:
    if s is None:
        return {}
    try:
        if isinstance(s, (bytes, bytearray)):
            s = s.decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        txt = str(s).strip()
        if not txt:
            return {}
        return json.loads(txt)
    except Exception:
        return {}


def _extract_model_name_from_model_json(model_json: Any) -> str:
    mj = _try_json_load(model_json)
    for k in ("model_name", "name", "model", "id"):
        v = mj.get(k)
        if v:
            return str(v).strip()
    return ""


def _extract_model_kind_from_model_json(model_json: Any) -> Optional[str]:
    mj = _try_json_load(model_json)
    for k in ("model_kind", "kind", "type"):
        v = mj.get(k)
        if v:
            return str(v).strip()
    return None


def _extract_model_ts_ms_from_model_json(model_json: Any) -> Optional[int]:
    mj = _try_json_load(model_json)
    for k in ("model_ts_ms", "ts_ms", "trained_ts_ms"):
        v = mj.get(k)
        if v is not None:
            try:
                return int(v)
            except Exception:
                pass
    return None


def _stddev(vals: List[float]) -> float:
    if not vals:
        return 0.0
    if len(vals) == 1:
        return 0.0
    m = sum(vals) / float(len(vals))
    var = sum((x - m) ** 2 for x in vals) / float(len(vals) - 1)
    return math.sqrt(max(0.0, var))


def _compute_drawdown_proxy(con, since_ms: int) -> Optional[float]:
    """
    Best-effort drawdown proxy:
    - prefer portfolio_bt_points.drawdown if exists
    - else equity_history max drawdown over window
    Returns positive magnitude (e.g. 0.12 for 12% dd) when possible.
    """
    now_ms = _now_ms()

    # 1) portfolio_bt_points.drawdown
    if _table_exists(con, "portfolio_bt_points"):
        try:
            rows = con.execute(
                """
                SELECT drawdown
                FROM portfolio_bt_points
                WHERE ts_ms >= ?
                ORDER BY ts_ms ASC
                """,
                (int(since_ms),),
            ).fetchall()
            dds = []
            for r in rows or []:
                dd = _safe_float(r[0], None)
                if dd is None:
                    continue
                # expect dd as positive fraction; if negative, normalize to abs
                dds.append(abs(float(dd)))
            if dds:
                return float(max(dds))
        except Exception:
            pass

    # 2) equity_history
    if _table_exists(con, "equity_history"):
        try:
            rows = con.execute(
                """
                SELECT ts_ms, equity
                FROM equity_history
                WHERE ts_ms >= ?
                ORDER BY ts_ms ASC
                """,
                (int(since_ms),),
            ).fetchall()
            eq = []
            for r in rows or []:
                v = _safe_float(r[1], None)
                if v is None:
                    continue
                eq.append(float(v))
            if len(eq) >= 2:
                peak = eq[0]
                max_dd = 0.0
                for v in eq:
                    peak = max(peak, v)
                    if peak > 0:
                        dd = (peak - v) / peak
                        max_dd = max(max_dd, dd)
                return float(max_dd)
        except Exception:
            pass

    return None


def _read_shadow_metrics(con, since_ms: int, regime: str) -> Dict[str, Dict[str, Any]]:
    """
    Returns per model_name:
      {rmse, dir_acc, net_rmse, n}
    Uses latest row per model_name in window.
    """
    out: Dict[str, Dict[str, Any]] = {}

    if not _table_exists(con, "shadow_metrics"):
        return out

    try:
        rows = con.execute(
            """
            SELECT window_end_ms, regime, model_name, horizon_s, rmse, dir_acc, net_rmse, n, extra_json
            FROM shadow_metrics
            WHERE window_end_ms >= ?
            ORDER BY window_end_ms DESC
            LIMIT 5000
            """,
            (int(since_ms),),
        ).fetchall()
    except Exception:
        rows = []

    for r in rows or []:
        try:
            reg = str(r[1] or "global")
            if str(regime) != "global" and reg != str(regime):
                continue
            name = str(r[2] or "").strip()
            if not name:
                continue

            # keep latest per model_name
            if name in out:
                continue

            out[name] = {
                "rmse": _safe_float(r[4], None),
                "dir_acc": _safe_float(r[5], None),
                "net_rmse": _safe_float(r[6], None),
                "n": _safe_int(r[7], 0),
            }
        except Exception:
            continue

    return out


def _read_slippage_by_model(con, since_ms: int) -> Dict[str, Dict[str, Any]]:
    """
    Reads trade_attribution_ledger slippage_bps by model (best-effort parse model_json).
    Returns:
      model_name -> {n, mean, std}
    """
    out: Dict[str, Dict[str, Any]] = {}

    if not _table_exists(con, "trade_attribution_ledger"):
        return out

    # Pull recent rows (bounded)
    try:
        rows = con.execute(
            f"""
            SELECT model_json, slippage_bps
            FROM trade_attribution_ledger
            WHERE ts_ms >= ?
            ORDER BY ts_ms DESC
            LIMIT {int(MAX_ROWS_ATTR)}
            """,
            (int(since_ms),),
        ).fetchall()
    except Exception:
        rows = []

    buf: Dict[str, List[float]] = {}

    for r in rows or []:
        try:
            name = _extract_model_name_from_model_json(r[0])
            if not name:
                continue
            slip = _safe_float(r[1], None)
            if slip is None:
                continue
            buf.setdefault(name, []).append(float(slip))
        except Exception:
            continue

    for name, vals in buf.items():
        if not vals:
            continue
        m = sum(vals) / float(len(vals))
        out[name] = {
            "n": int(len(vals)),
            "mean": float(m),
            "std": float(_stddev(vals)),
        }

    return out


def _read_cap_eff_by_model(con, since_ms: int) -> Dict[str, Dict[str, Any]]:
    """
    Capital efficiency proxy using shadow_predictions:
      cap_eff ~ mean(net_pred_z) / (1 + mean(cost_est))
    If net_pred_z missing, use predicted_z.
    Returns:
      model_name -> {n, cap_eff, mean_cost, mean_edge}
    """
    out: Dict[str, Dict[str, Any]] = {}

    if not _table_exists(con, "shadow_predictions"):
        return out

    try:
        rows = con.execute(
            """
            SELECT model_name, predicted_z, net_pred_z, cost_est
            FROM shadow_predictions
            WHERE ts_ms >= ?
            ORDER BY ts_ms DESC
            LIMIT 200000
            """,
            (int(since_ms),),
        ).fetchall()
    except Exception:
        rows = []

    agg: Dict[str, Dict[str, float]] = {}
    cnt: Dict[str, int] = {}

    for r in rows or []:
        try:
            name = str(r[0] or "").strip()
            if not name:
                continue
            pred = _safe_float(r[1], None)
            netp = _safe_float(r[2], None)
            cost = _safe_float(r[3], 0.0)
            edge = netp if netp is not None else pred
            if edge is None:
                continue

            a = agg.setdefault(name, {"edge_sum": 0.0, "cost_sum": 0.0})
            a["edge_sum"] += float(edge)
            a["cost_sum"] += float(cost or 0.0)
            cnt[name] = cnt.get(name, 0) + 1
        except Exception:
            continue

    for name, a in agg.items():
        n = int(cnt.get(name, 0))
        if n <= 0:
            continue
        mean_edge = float(a["edge_sum"] / float(n))
        mean_cost = float(a["cost_sum"] / float(n))
        cap_eff = float(mean_edge / (1.0 + max(0.0, mean_cost)))
        out[name] = {
            "n": n,
            "cap_eff": cap_eff,
            "mean_cost": mean_cost,
            "mean_edge": mean_edge,
        }

    return out


def compute_and_persist_shadow_capital_scores(
    *,
    window_s: int = DEFAULT_WINDOW_S,
    regime: str = DEFAULT_REGIME,
    min_n: int = MIN_N,
) -> Dict[str, Any]:
    """
    Computes and upserts shadow_capital_scores for window_s/regime.

    Composite score (higher better):
      + W_DIR_ACC * dir_acc
      - W_NET_RMSE * net_rmse
      - W_SLIP_MEAN * slip_mean
      - W_SLIP_STD * slip_std
      - W_DD * drawdown_proxy
      + W_CAP_EFF * cap_eff
    """
    now_ms = _now_ms()
    since_ms = now_ms - int(max(60, int(window_s)) * 1000)

    con = _db_connect()
    try:
        # Required table
        if not _table_exists(con, "shadow_capital_scores"):
            return {"ok": False, "error": "shadow_capital_scores table missing (run init_db?)"}

        # Inputs (best-effort)
        sm = _read_shadow_metrics(con, since_ms=since_ms, regime=str(regime))
        slip = _read_slippage_by_model(con, since_ms=since_ms)
        cap = _read_cap_eff_by_model(con, since_ms=since_ms)
        dd = _compute_drawdown_proxy(con, since_ms=since_ms)

        weights = {
            "W_DIR_ACC": W_DIR_ACC,
            "W_NET_RMSE": W_NET_RMSE,
            "W_SLIP_MEAN": W_SLIP_MEAN,
            "W_SLIP_STD": W_SLIP_STD,
            "W_DD": W_DD,
            "W_CAP_EFF": W_CAP_EFF,
        }

        upserts = 0
        skipped = 0

        for model_name, m in (sm or {}).items():
            n = int(m.get("n") or 0)

            # tighten using other sources when available
            n = max(n, int((cap.get(model_name) or {}).get("n") or 0))
            n = max(n, int((slip.get(model_name) or {}).get("n") or 0))

            if n < int(min_n):
                skipped += 1
                continue

            rmse = _safe_float(m.get("rmse"), None)
            dir_acc = _safe_float(m.get("dir_acc"), None)
            net_rmse = _safe_float(m.get("net_rmse"), None)

            slip_mean = _safe_float((slip.get(model_name) or {}).get("mean"), 0.0)
            slip_std = _safe_float((slip.get(model_name) or {}).get("std"), 0.0)

            cap_eff = _safe_float((cap.get(model_name) or {}).get("cap_eff"), 0.0)

            drawdown_proxy = _safe_float(dd, 0.0) if dd is not None else 0.0

            # normalize missings to conservative
            if dir_acc is None:
                dir_acc = 0.0
            if net_rmse is None:
                net_rmse = rmse if rmse is not None else 0.0

            score = (
                float(W_DIR_ACC) * float(dir_acc)
                - float(W_NET_RMSE) * float(net_rmse)
                - float(W_SLIP_MEAN) * float(slip_mean or 0.0)
                - float(W_SLIP_STD) * float(slip_std or 0.0)
                - float(W_DD) * float(drawdown_proxy or 0.0)
                + float(W_CAP_EFF) * float(cap_eff or 0.0)
            )

            components = {
                "dir_acc": dir_acc,
                "net_rmse": net_rmse,
                "slip_mean": slip_mean,
                "slip_std": slip_std,
                "drawdown_proxy": drawdown_proxy,
                "cap_eff": cap_eff,
            }

            try:
                con.execute(
                    """
                    INSERT INTO shadow_capital_scores
                      (ts_ms, window_s, regime, model_name, model_kind, model_ts_ms,
                       n, rmse, dir_acc, net_rmse,
                       slippage_bps_mean, slippage_bps_std, drawdown_proxy,
                       cap_eff, score, weights_json, components_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(model_name, window_s, regime) DO UPDATE SET
                      ts_ms=excluded.ts_ms,
                      n=excluded.n,
                      rmse=excluded.rmse,
                      dir_acc=excluded.dir_acc,
                      net_rmse=excluded.net_rmse,
                      slippage_bps_mean=excluded.slippage_bps_mean,
                      slippage_bps_std=excluded.slippage_bps_std,
                      drawdown_proxy=excluded.drawdown_proxy,
                      cap_eff=excluded.cap_eff,
                      score=excluded.score,
                      weights_json=excluded.weights_json,
                      components_json=excluded.components_json
                    """,
                    (
                        int(now_ms),
                        int(window_s),
                        str(regime),
                        str(model_name),
                        None,
                        None,
                        int(n),
                        rmse,
                        dir_acc,
                        net_rmse,
                        slip_mean,
                        slip_std,
                        drawdown_proxy,
                        cap_eff,
                        float(score),
                        json.dumps(weights, separators=(",", ":"), sort_keys=True),
                        json.dumps(components, separators=(",", ":"), sort_keys=True),
                    ),
                )
                upserts += 1
            except Exception:
                skipped += 1
                continue

        try:
            con.commit()
        except Exception:
            pass

        return {
            "ok": True,
            "ts_ms": int(now_ms),
            "window_s": int(window_s),
            "regime": str(regime),
            "upserts": int(upserts),
            "skipped": int(skipped),
            "drawdown_proxy": dd,
            "weights": weights,
        }
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_shadow_capital_scores(limit: int = 50, regime: str = DEFAULT_REGIME) -> Dict[str, Any]:
    limit = max(1, min(500, int(limit or 50)))
    con = _db_connect()
    try:
        if not _table_exists(con, "shadow_capital_scores"):
            return {"ok": True, "rows": []}

        try:
            rows = con.execute(
                """
                SELECT ts_ms, window_s, regime, model_name, n,
                       rmse, dir_acc, net_rmse,
                       slippage_bps_mean, slippage_bps_std, drawdown_proxy,
                       cap_eff, score, components_json
                FROM shadow_capital_scores
                WHERE regime=?
                ORDER BY score DESC
                LIMIT ?
                """,
                (str(regime), int(limit)),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            try:
                out.append(
                    {
                        "ts_ms": int(r[0] or 0),
                        "window_s": int(r[1] or 0),
                        "regime": str(r[2] or "global"),
                        "model_name": str(r[3] or ""),
                        "n": int(r[4] or 0),
                        "rmse": _safe_float(r[5], None),
                        "dir_acc": _safe_float(r[6], None),
                        "net_rmse": _safe_float(r[7], None),
                        "slippage_bps_mean": _safe_float(r[8], None),
                        "slippage_bps_std": _safe_float(r[9], None),
                        "drawdown_proxy": _safe_float(r[10], None),
                        "cap_eff": _safe_float(r[11], None),
                        "score": _safe_float(r[12], None),
                        "components": _try_json_load(r[13]),
                    }
                )
            except Exception:
                continue

        return {"ok": True, "rows": out}
    finally:
        try:
            con.close()
        except Exception:
            pass
