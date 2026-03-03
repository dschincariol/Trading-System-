# engine/runtime/strategy_allocator.py
"""
Strategy Allocator (Meta Capital Engine)

Computes rolling, drawdown-aware, correlation-adjusted capital weights per strategy.

Inputs:
- execution_capital_efficiency (preferred)  [engine/execution/execution_ledger.py]

Persists:
- strategy_metrics (window_days=0)   metrics_json includes allocator fields
- strategy_allocations (window_days=0) allocations_json includes normalized weights

Fail-open:
- If required tables are missing, returns empty allocations and does not raise.

Env (optional):
  STRATEGY_ALLOC_WINDOW_S=86400
  STRATEGY_ALLOC_BUCKET_S=900
  STRATEGY_ALLOC_CORR_GAMMA=1.5
  STRATEGY_ALLOC_DD_TH=0.10
  STRATEGY_ALLOC_DD_FLOOR=0.10
  STRATEGY_ALLOC_MIN_SHARE=0.0
  STRATEGY_ALLOC_MAX_SHARE=1.0
  STRATEGY_ALLOC_RISK_BUDGETS_JSON='{"baseline":0.60,"conservative":0.40}'
  STRATEGY_ALLOC_SCORE_FLOOR=0.0

NOTE:
- This module does NOT change trade generation.
- It only produces weights for capital allocation across strategies.
"""

import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_WINDOW_S = int(os.environ.get("STRATEGY_ALLOC_WINDOW_S", "86400"))
DEFAULT_BUCKET_S = int(os.environ.get("STRATEGY_ALLOC_BUCKET_S", "900"))

CORR_GAMMA = float(os.environ.get("STRATEGY_ALLOC_CORR_GAMMA", "1.5"))

DD_TH = float(os.environ.get("STRATEGY_ALLOC_DD_TH", "0.10"))
DD_FLOOR = float(os.environ.get("STRATEGY_ALLOC_DD_FLOOR", "0.10"))

MIN_SHARE = float(os.environ.get("STRATEGY_ALLOC_MIN_SHARE", "0.0"))
MAX_SHARE = float(os.environ.get("STRATEGY_ALLOC_MAX_SHARE", "1.0"))

SCORE_FLOOR = float(os.environ.get("STRATEGY_ALLOC_SCORE_FLOOR", "0.0"))

_RISK_BUDGETS_RAW = os.environ.get("STRATEGY_ALLOC_RISK_BUDGETS_JSON", "").strip()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        v = float(x)
        if not math.isfinite(v):
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _table_exists(con, name: str) -> bool:
    try:
        r = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(name),),
        ).fetchone()
        return bool(r)
    except Exception:
        return False


def _parse_risk_budgets() -> Dict[str, float]:
    if not _RISK_BUDGETS_RAW:
        return {}
    try:
        obj = json.loads(_RISK_BUDGETS_RAW)
        if not isinstance(obj, dict):
            return {}
        out: Dict[str, float] = {}
        for k, v in obj.items():
            try:
                kk = str(k).strip()
                if not kk:
                    continue
                out[kk] = max(0.0, float(v))
            except Exception:
                continue
        return out
    except Exception:
        return {}


def _bucket_ts(ts_ms: int, bucket_s: int) -> int:
    b = int(max(1, int(bucket_s)))
    return int((int(ts_ms) // int(b * 1000)) * int(b * 1000))


def _stddev(vals: List[float]) -> float:
    if not vals:
        return 0.0
    if len(vals) == 1:
        return 0.0
    m = sum(vals) / float(len(vals))
    var = sum((x - m) ** 2 for x in vals) / float(len(vals) - 1)
    return math.sqrt(max(0.0, var))


def _max_drawdown_from_pnl_series(pnl_series: List[Tuple[int, float]]) -> float:
    """pnl_series: list of (bucket_ts_ms, pnl) sorted by ts.
    Returns positive fraction-like magnitude computed on cumulative pnl.
    """
    if not pnl_series:
        return 0.0

    eq = 0.0
    peak = 0.0
    max_dd = 0.0

    for _, pnl in pnl_series:
        eq += float(pnl)
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd

    # normalize by (peak abs) to get a stable fraction-like number
    denom = max(1e-9, abs(float(peak)))
    return float(max_dd) / float(denom)


def _corr(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b) or len(a) < 3:
        return 0.0
    ma = sum(a) / len(a)
    mb = sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 1e-12 or vb <= 1e-12:
        return 0.0
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(len(a)))
    return float(cov / math.sqrt(va * vb))


def _read_exec_cap_eff(con, since_ms: int, now_ms: int) -> List[Tuple[int, str, float, float, float]]:
    """Returns rows: (ts_ms, strategy_name, pnl_net, return_per_risk, drawdown_contrib)

    Uses execution_capital_efficiency if present.
    """
    if not _table_exists(con, "execution_capital_efficiency"):
        return []

    try:
        rows = con.execute(
            """
            SELECT ts_ms, strategy_name, pnl_net, return_per_risk, drawdown_contrib
            FROM execution_capital_efficiency
            WHERE ts_ms BETWEEN ? AND ?
            ORDER BY ts_ms ASC
            """,
            (int(since_ms), int(now_ms)),
        ).fetchall()
    except Exception:
        return []

    out: List[Tuple[int, str, float, float, float]] = []
    for r in rows or []:
        try:
            ts_ms = int(r[0] or 0)
            name = str(r[1] or "").strip()
            if not name:
                continue
            pnl_net = _safe_float(r[2], 0.0)
            rpr = _safe_float(r[3], 0.0)
            dd_c = _safe_float(r[4], 0.0)
            out.append((ts_ms, name, pnl_net, rpr, dd_c))
        except Exception:
            continue

    return out


def compute_and_persist_strategy_allocations(con, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Computes allocations and persists into strategy_metrics + strategy_allocations.

    Returns:
      {
        "ok": bool,
        "ts_ms": int,
        "window_s": int,
        "bucket_s": int,
        "allocations": {strategy_name: weight},
        "details": {strategy_name: {...metrics...}},
      }
    """
    ts_ms = int(now_ms) if now_ms is not None else _now_ms()
    window_s = int(max(60, int(DEFAULT_WINDOW_S)))
    bucket_s = int(max(60, int(DEFAULT_BUCKET_S)))

    since_ms = int(ts_ms) - int(window_s * 1000)

    rows = _read_exec_cap_eff(con, since_ms=since_ms, now_ms=ts_ms)
    if not rows:
        return {
            "ok": False,
            "ts_ms": int(ts_ms),
            "window_s": int(window_s),
            "bucket_s": int(bucket_s),
            "allocations": {},
            "details": {},
            "reason": "no_execution_capital_efficiency_rows",
        }

    # Bucket returns per strategy
    by_strat_bucket: Dict[str, Dict[int, float]] = {}
    by_strat_rpr: Dict[str, List[float]] = {}
    by_strat_ddc: Dict[str, List[float]] = {}

    for r_ts_ms, name, pnl_net, rpr, dd_c in rows:
        bts = _bucket_ts(int(r_ts_ms), bucket_s=bucket_s)
        by_strat_bucket.setdefault(name, {})
        by_strat_bucket[name][bts] = float(by_strat_bucket[name].get(bts, 0.0)) + float(pnl_net)
        by_strat_rpr.setdefault(name, []).append(float(rpr))
        by_strat_ddc.setdefault(name, []).append(float(dd_c))

    strategies = sorted(by_strat_bucket.keys())

    # Build aligned bucket return matrix for correlation
    all_buckets = sorted({b for m in by_strat_bucket.values() for b in m.keys()})

    series: Dict[str, List[float]] = {}
    pnl_series: Dict[str, List[Tuple[int, float]]] = {}

    for s in strategies:
        m = by_strat_bucket.get(s) or {}
        series[s] = [float(m.get(b, 0.0)) for b in all_buckets]
        pnl_series[s] = [(int(b), float(m.get(b, 0.0))) for b in all_buckets]

    # Correlation penalty per strategy
    corr_penalty: Dict[str, float] = {}
    avg_abs_corr: Dict[str, float] = {}

    for i, si in enumerate(strategies):
        abs_corrs = []
        for j, sj in enumerate(strategies):
            if i == j:
                continue
            c = _corr(series[si], series[sj])
            abs_corrs.append(abs(float(c)))
        ac = sum(abs_corrs) / float(len(abs_corrs)) if abs_corrs else 0.0
        avg_abs_corr[si] = float(ac)
        pen = 1.0 / (1.0 + max(0.0, float(CORR_GAMMA)) * float(ac))
        corr_penalty[si] = float(max(0.0, min(1.0, pen)))

    # Risk budgets (0..1) per strategy
    budgets = _parse_risk_budgets()

    details: Dict[str, Dict[str, Any]] = {}
    raw_scores: Dict[str, float] = {}

    for s in strategies:
        rets = series.get(s) or []
        mu = sum(rets) / float(len(rets)) if rets else 0.0
        sd = _stddev(rets)
        sharpe = float(mu / sd) if sd > 1e-12 else 0.0

        dd = _max_drawdown_from_pnl_series(pnl_series.get(s) or [])

        # drawdown-aware scale: linearly compress beyond DD_TH down to DD_FLOOR
        dd_th = max(1e-6, float(DD_TH))
        if dd <= dd_th:
            dd_scale = 1.0
        else:
            dd_scale = max(float(DD_FLOOR), 1.0 - ((float(dd) - dd_th) / dd_th))

        rpr_vals = by_strat_rpr.get(s) or []
        rpr_mean = sum(rpr_vals) / float(len(rpr_vals)) if rpr_vals else 0.0

        ddc_vals = by_strat_ddc.get(s) or []
        ddc_mean = sum(ddc_vals) / float(len(ddc_vals)) if ddc_vals else 0.0

        # base perf score (rolling): sharpe + return_per_risk_unit
        base = float(sharpe) + float(rpr_mean)

        # apply dd + corr penalties
        score = float(base) * float(dd_scale) * float(corr_penalty.get(s, 1.0))

        # risk budget (cap share via multiplicative budget)
        budget = float(budgets.get(s, 1.0))
        if budget < 0.0:
            budget = 0.0
        score *= float(budget)

        if float(score) < float(SCORE_FLOOR):
            score = float(SCORE_FLOOR)

        raw_scores[s] = float(max(0.0, score))

        details[s] = {
            "window_s": int(window_s),
            "bucket_s": int(bucket_s),
            "n_rows": int(sum(1 for _ in (by_strat_rpr.get(s) or []))),
            "mean_bucket_pnl": float(mu),
            "std_bucket_pnl": float(sd),
            "sharpe_bucket": float(sharpe),
            "max_drawdown_proxy": float(dd),
            "dd_scale": float(dd_scale),
            "avg_abs_corr": float(avg_abs_corr.get(s, 0.0)),
            "corr_penalty": float(corr_penalty.get(s, 1.0)),
            "return_per_risk_unit": float(rpr_mean),
            "drawdown_contribution": float(ddc_mean),
            "risk_budget": float(budget),
            "raw_score": float(raw_scores[s]),
        }

    # Normalize into allocations
    total = sum(float(v) for v in raw_scores.values())
    if total <= 1e-12:
        # fail-open equal weights
        total = float(len(strategies) or 1)
        for s in strategies:
            raw_scores[s] = 1.0

    alloc = {s: float(raw_scores[s]) / float(total) for s in strategies}

    # Apply min/max share clamps, then renormalize
    if strategies:
        for s in strategies:
            w = float(alloc.get(s, 0.0))
            w = max(float(MIN_SHARE), min(float(MAX_SHARE), float(w)))
            alloc[s] = float(w)

        gross = sum(float(alloc.get(s, 0.0)) for s in strategies)
        if gross > 1e-12:
            for s in strategies:
                alloc[s] = float(alloc.get(s, 0.0)) / float(gross)

    # Persist strategy_metrics + strategy_allocations (window_days=0)
    try:
        for s in strategies:
            mj = dict(details.get(s) or {})
            mj["efficiency_score"] = float(mj.get("raw_score", 0.0))

            con.execute(
                """
                INSERT INTO strategy_metrics(strategy_name, window_days, ts_ms, metrics_json, is_active)
                VALUES (?,?,?,?,?)
                ON CONFLICT(strategy_name, window_days) DO UPDATE SET
                  ts_ms=excluded.ts_ms,
                  metrics_json=excluded.metrics_json,
                  is_active=excluded.is_active
                """,
                (str(s), 0, int(ts_ms), json.dumps(mj, separators=(",", ":"), sort_keys=True), 1),
            )

        con.execute(
            """
            INSERT INTO strategy_allocations(ts_ms, window_days, allocations_json, reason_json)
            VALUES (?,?,?,?)
            ON CONFLICT(ts_ms, window_days) DO UPDATE SET
              allocations_json=excluded.allocations_json,
              reason_json=excluded.reason_json
            """,
            (
                int(ts_ms),
                0,
                json.dumps(alloc, separators=(",", ":"), sort_keys=True),
                json.dumps(
                    {
                        "window_s": int(window_s),
                        "bucket_s": int(bucket_s),
                        "strategies": strategies,
                        "risk_budgets": budgets,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
    except Exception:
        # best-effort; never fail the caller
        pass

    return {
        "ok": True,
        "ts_ms": int(ts_ms),
        "window_s": int(window_s),
        "bucket_s": int(bucket_s),
        "allocations": dict(alloc),
        "details": dict(details),
    }