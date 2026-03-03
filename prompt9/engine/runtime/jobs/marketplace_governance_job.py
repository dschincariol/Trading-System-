import json
import os
import time
import math
from typing import Dict, Any, List, Tuple

from engine.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
)


LOCK_NAME = os.environ.get("MARKETPLACE_LOCK_NAME", "marketplace_governance_job")
LOCK_STALE_S = int(os.environ.get("MARKETPLACE_LOCK_STALE_S", "600"))

# Decision windows (days)
WINDOW_DAYS = [int(x) for x in os.environ.get("MARKETPLACE_WINDOWS_DAYS", "20,60").split(",") if str(x).strip()]

# Hard gates (fail-closed)
MIN_N = int(os.environ.get("MP_MIN_N", "50"))
MIN_DAYS = int(os.environ.get("MP_MIN_DAYS", "5"))
MIN_NET_PNL = float(os.environ.get("MP_MIN_NET_PNL", "0.0"))
MAX_DRAWDOWN = float(os.environ.get("MP_MAX_DRAWDOWN", "1e9"))
MAX_COST_RATIO = float(os.environ.get("MP_MAX_COST_RATIO", "0.80"))

# Scoring weights
W_NET_PNL = float(os.environ.get("MP_W_NET_PNL", "1.0"))
W_SHARPE = float(os.environ.get("MP_W_SHARPE", "0.5"))
W_DD = float(os.environ.get("MP_W_DD", "1.0"))
W_COST = float(os.environ.get("MP_W_COST", "0.5"))
W_STAB = float(os.environ.get("MP_W_STAB", "0.5"))

# Allocation controls
TOP_K_PER_SYMBOL_HORIZON = int(os.environ.get("MP_TOP_K_PER_BUCKET", "1"))
MAX_SYMBOLS_ACTIVE = int(os.environ.get("MP_MAX_SYMBOLS_ACTIVE", "250"))
ACTIVATE_SCORE_THRESHOLD = float(os.environ.get("MP_ACTIVATE_SCORE_TH", "0.0"))

# Training budget
TOTAL_BUDGET_UNITS = float(os.environ.get("MP_TRAINING_BUDGET_UNITS", "100.0"))
EXPLORE_FRACTION = float(os.environ.get("MP_EXPLORE_FRACTION", "0.20"))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_float(x, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        v = float(x)
        if v != v:
            return float(default)
        return v
    except Exception:
        return float(default)


def _safe_int(x, default: int = 0) -> int:
    try:
        if x is None:
            return int(default)
        return int(x)
    except Exception:
        return int(default)


def _parse_model_name(model_json: str) -> str:
    if not model_json:
        return ""
    try:
        mj = json.loads(model_json)
        if not isinstance(mj, dict):
            return ""
    except Exception:
        return ""

    for k in ("model_name", "name", "model"):
        v = mj.get(k)
        if v:
            return str(v).strip()

    # explain-json style keys
    for k in ("model.name", "model_meta.name"):
        v = mj.get(k)
        if v:
            return str(v).strip()

    return ""


def _parse_horizon_s(model_json: str, signal_json: str) -> int:
    # prefer explicit in signal_json
    try:
        if signal_json:
            sj = json.loads(signal_json)
            if isinstance(sj, dict) and sj.get("horizon_s") is not None:
                return int(sj.get("horizon_s"))
    except Exception:
        pass

    try:
        if model_json:
            mj = json.loads(model_json)
            if isinstance(mj, dict) and mj.get("horizon_s") is not None:
                return int(mj.get("horizon_s"))
    except Exception:
        pass

    return 0


def _std(vals: List[float]) -> float:
    if not vals or len(vals) < 2:
        return 0.0
    m = sum(vals) / float(len(vals))
    var = sum((x - m) ** 2 for x in vals) / float(len(vals) - 1)
    return math.sqrt(max(0.0, var))


def _compute_max_drawdown_from_cum(cum: List[float]) -> float:
    if not cum:
        return 0.0
    peak = cum[0]
    max_dd = 0.0
    for v in cum:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return float(max_dd)


def _stability_from_blocks(block_sums: List[float]) -> float:
    # Stability in [-1,1] = fraction of blocks positive mapped to [-1,1]
    if not block_sums:
        return 0.0
    pos = sum(1 for x in block_sums if x > 0)
    frac = float(pos) / float(len(block_sums))
    return float(frac * 2.0 - 1.0)


def _score_row(net_pnl: float, sharpe_like: float, max_dd: float, cost_ratio: float, stability: float) -> float:
    # higher is better
    return (
        float(W_NET_PNL) * float(net_pnl)
        + float(W_SHARPE) * float(sharpe_like)
        - float(W_DD) * float(max_dd)
        - float(W_COST) * float(cost_ratio)
        + float(W_STAB) * float(stability)
    )


def _gates(n: int, n_days: int, net_pnl: float, max_dd: float, cost_ratio: float) -> Tuple[bool, str]:
    if int(n) < int(MIN_N):
        return False, "MIN_N"
    if int(n_days) < int(MIN_DAYS):
        return False, "MIN_DAYS"
    if float(net_pnl) < float(MIN_NET_PNL):
        return False, "MIN_NET_PNL"
    if float(max_dd) > float(MAX_DRAWDOWN):
        return False, "MAX_DRAWDOWN"
    if float(cost_ratio) > float(MAX_COST_RATIO):
        return False, "MAX_COST_RATIO"
    return True, "OK"


def _iter_trade_attr_rows(con, since_ms: int):
    # Read pnl rows with model attribution. Ignore suppressions and null pnl.
    return con.execute(
        """
        SELECT ts_ms, symbol, signal_json, model_json, pnl, fees
        FROM trade_attribution_ledger
        WHERE ts_ms >= ?
          AND suppression_reason IS NULL
          AND pnl IS NOT NULL
        ORDER BY ts_ms ASC
        """,
        (int(since_ms),),
    ).fetchall()


def _aggregate(con, *, window_days: int, asof_ms: int) -> Tuple[Dict[Tuple[str, int, str], Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    start_ms = int(asof_ms) - int(window_days) * 86400 * 1000
    rows = _iter_trade_attr_rows(con, start_ms)

    by_key: Dict[Tuple[str, int, str], Dict[str, Any]] = {}

    for ts_ms, symbol, signal_json, model_json, pnl, fees in rows or []:
        sym = str(symbol or "").strip().upper()
        if not sym:
            continue

        model_name = _parse_model_name(model_json or "")
        if not model_name:
            model_name = "unknown"

        horizon_s = _parse_horizon_s(model_json or "", signal_json or "")
        if horizon_s <= 0:
            horizon_s = 0

        k = (sym, int(horizon_s), str(model_name))
        rec = by_key.get(k)
        if rec is None:
            rec = {
                "pnl": [],
                "fees": [],
                "days": set(),
            }
            by_key[k] = rec

        rec["pnl"].append(_safe_float(pnl, 0.0))
        rec["fees"].append(_safe_float(fees, 0.0))
        # deterministic day bucket
        day = int(ts_ms) // (86400 * 1000)
        rec["days"].add(int(day))

    model_out: Dict[Tuple[str, int, str], Dict[str, Any]] = {}
    symbol_out: Dict[str, Dict[str, Any]] = {}

    for (sym, horizon_s, model_name), rec in by_key.items():
        pnls = rec["pnl"]
        fees = rec["fees"]
        n = int(len(pnls))
        n_days = int(len(rec["days"]))

        net_pnl = float(sum(pnls) - sum(fees))
        fees_sum = float(sum(fees))

        # sharpe_like on per-trade pnls (deterministic, no annualization)
        mu = float(sum(pnls) / float(n)) if n > 0 else 0.0
        vol = float(_std(pnls))
        sharpe_like = float(mu / vol) if vol > 1e-12 else 0.0

        # max drawdown on cumulative net pnl
        cum = []
        c = 0.0
        for p, f in zip(pnls, fees):
            c += float(p) - float(f)
            cum.append(float(c))
        max_dd = _compute_max_drawdown_from_cum(cum)

        # stability: 3 equal blocks
        blocks = [0.0, 0.0, 0.0]
        if n > 0:
            for i, (p, f) in enumerate(zip(pnls, fees)):
                bi = min(2, int(i * 3 / max(1, n)))
                blocks[bi] += float(p) - float(f)
        stability = _stability_from_blocks(blocks)

        gross_abs = float(sum(abs(float(p)) for p in pnls))
        cost_ratio = float((fees_sum / gross_abs) if gross_abs > 1e-12 else 0.0)

        score = _score_row(net_pnl=net_pnl, sharpe_like=sharpe_like, max_dd=max_dd, cost_ratio=cost_ratio, stability=stability)
        ok, reason = _gates(n=n, n_days=n_days, net_pnl=net_pnl, max_dd=max_dd, cost_ratio=cost_ratio)

        model_out[(sym, horizon_s, model_name)] = {
            "symbol": sym,
            "horizon_s": int(horizon_s),
            "model_name": str(model_name),
            "n": int(n),
            "n_days": int(n_days),
            "net_pnl": float(net_pnl),
            "fees": float(fees_sum),
            "sharpe_like": float(sharpe_like),
            "max_drawdown": float(max_dd),
            "stability": float(stability),
            "cost_ratio": float(cost_ratio),
            "score": float(score),
            "eligible": bool(ok),
            "reason": str(reason),
        }

    # Aggregate to symbol score using best model score per (symbol,horizon)
    best_by_symbol: Dict[str, Dict[str, Any]] = {}

    for (sym, horizon_s, model_name), m in model_out.items():
        cur = best_by_symbol.get(sym)
        if cur is None or float(m["score"]) > float(cur["score"]):
            best_by_symbol[sym] = dict(m)

    for sym, best in best_by_symbol.items():
        symbol_out[sym] = {
            "symbol": sym,
            "n": int(best.get("n") or 0),
            "n_days": int(best.get("n_days") or 0),
            "net_pnl": float(best.get("net_pnl") or 0.0),
            "sharpe_like": float(best.get("sharpe_like") or 0.0),
            "max_drawdown": float(best.get("max_drawdown") or 0.0),
            "stability": float(best.get("stability") or 0.0),
            "score": float(best.get("score") or 0.0),
            "best_horizon_s": int(best.get("horizon_s") or 0),
            "best_model_name": str(best.get("model_name") or ""),
            "detail": {
                "best": best,
            },
        }

    return model_out, symbol_out


def _upsert_scores_and_states(con, *, ts_ms: int, window_days: int, model_out: Dict[Tuple[str, int, str], Dict[str, Any]], symbol_out: Dict[str, Dict[str, Any]]) -> None:
    for _, m in model_out.items():
        con.execute(
            """
            INSERT OR REPLACE INTO marketplace_model_score(
              ts_ms, window_days,
              symbol, horizon_s, model_name,
              n, n_days, net_pnl, fees,
              sharpe_like, max_drawdown, stability, cost_ratio,
              score, gates_json
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(ts_ms),
                int(window_days),
                str(m["symbol"]),
                int(m["horizon_s"]),
                str(m["model_name"]),
                int(m["n"]),
                int(m["n_days"]),
                float(m["net_pnl"]),
                float(m["fees"]),
                float(m["sharpe_like"]),
                float(m["max_drawdown"]),
                float(m["stability"]),
                float(m["cost_ratio"]),
                float(m["score"]),
                json.dumps({"eligible": bool(m["eligible"]), "reason": str(m["reason"])}, separators=(",", ":"), sort_keys=True),
            ),
        )

        # state is based on eligibility only; tiers set later by ranking
        con.execute(
            """
            INSERT OR IGNORE INTO marketplace_model_state(
              symbol, horizon_s, model_name,
              tier, eligible, reason_blocked,
              promoted_ts_ms, demoted_ts_ms,
              last_decision_ts_ms
            )
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                str(m["symbol"]),
                int(m["horizon_s"]),
                str(m["model_name"]),
                "paper",
                0,
                None,
                None,
                None,
                int(ts_ms),
            ),
        )

    for sym, s in symbol_out.items():
        con.execute(
            """
            INSERT OR REPLACE INTO marketplace_symbol_score(
              ts_ms, window_days, symbol,
              n, n_days, net_pnl,
              sharpe_like, max_drawdown, stability,
              score, best_horizon_s, best_model_name, detail_json
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(ts_ms),
                int(window_days),
                str(sym),
                int(s["n"]),
                int(s["n_days"]),
                float(s["net_pnl"]),
                float(s["sharpe_like"]),
                float(s["max_drawdown"]),
                float(s["stability"]),
                float(s["score"]),
                int(s["best_horizon_s"]),
                str(s["best_model_name"]),
                json.dumps(s.get("detail") or {}, separators=(",", ":"), sort_keys=True),
            ),
        )


def _set_universe_from_symbol_scores(con, *, ts_ms: int, window_days: int) -> Dict[str, Any]:
    # Pick top symbols by marketplace_symbol_score.score and mark them ACTIVE.
    rows = con.execute(
        """
        SELECT symbol, score
        FROM marketplace_symbol_score
        WHERE ts_ms=? AND window_days=?
        ORDER BY score DESC, symbol ASC
        LIMIT ?
        """,
        (int(ts_ms), int(window_days), int(MAX_SYMBOLS_ACTIVE)),
    ).fetchall()

    chosen = []
    for r in rows or []:
        sym = str(r[0] or "").strip().upper()
        sc = _safe_float(r[1], 0.0)
        if not sym:
            continue
        if float(sc) < float(ACTIVATE_SCORE_THRESHOLD):
            continue
        chosen.append((sym, float(sc)))

    # Update symbols.score and status deterministically.
    # NOTE: we only promote to ACTIVE; we do not mass-demote to avoid churn.
    now_ms = int(ts_ms)
    for sym, sc in chosen:
        con.execute(
            """
            INSERT INTO symbols(symbol, asset_class, status, score, last_seen_event_ts_ms, last_traded_ts_ms, meta_json, created_ts_ms, updated_ts_ms)
            VALUES(?, COALESCE((SELECT asset_class FROM symbols WHERE symbol=?), 'UNKNOWN'), 'ACTIVE', ?, NULL, NULL,
                   COALESCE((SELECT meta_json FROM symbols WHERE symbol=?), NULL), ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
              status='ACTIVE',
              score=excluded.score,
              updated_ts_ms=excluded.updated_ts_ms
            """,
            (str(sym), str(sym), float(sc), str(sym), int(now_ms), int(now_ms)),
        )

    return {"activated": int(len(chosen))}


def _rank_and_set_model_tiers(con, *, ts_ms: int, window_days: int) -> Dict[str, Any]:
    # For each (symbol,horizon), choose top-K eligible models by score.
    rows = con.execute(
        """
        SELECT symbol, horizon_s, model_name, score, gates_json
        FROM marketplace_model_score
        WHERE ts_ms=? AND window_days=?
        ORDER BY symbol ASC, horizon_s ASC, score DESC, model_name ASC
        """,
        (int(ts_ms), int(window_days)),
    ).fetchall()

    by_bucket: Dict[Tuple[str, int], List[Tuple[str, float, Dict[str, Any]]]] = {}
    for sym, h, m, sc, gates_json in rows or []:
        sym = str(sym or "").strip().upper()
        h = int(h or 0)
        m = str(m or "").strip()
        sc = _safe_float(sc, 0.0)
        try:
            gj = json.loads(gates_json or "{}")
            if not isinstance(gj, dict):
                gj = {}
        except Exception:
            gj = {}
        by_bucket.setdefault((sym, h), []).append((m, float(sc), gj))

    promoted = 0
    blocked = 0

    for (sym, h), lst in by_bucket.items():
        eligible = [x for x in lst if bool((x[2] or {}).get("eligible", False))]
        # ensure deterministic order
        eligible.sort(key=lambda x: (-float(x[1]), str(x[0])))

        winners = set(m for (m, _, _) in eligible[: int(TOP_K_PER_SYMBOL_HORIZON)])

        for (m, _, gj) in lst:
            ok = bool((gj or {}).get("eligible", False))
            if not ok:
                con.execute(
                    """
                    UPDATE marketplace_model_state
                    SET tier='blocked', eligible=0, reason_blocked=?, demoted_ts_ms=COALESCE(demoted_ts_ms, ?), last_decision_ts_ms=?
                    WHERE symbol=? AND horizon_s=? AND model_name=?
                    """,
                    (str((gj or {}).get("reason") or "GATE"), int(ts_ms), int(ts_ms), str(sym), int(h), str(m)),
                )
                blocked += 1
                continue

            if m in winners:
                con.execute(
                    """
                    UPDATE marketplace_model_state
                    SET tier='live', eligible=1, reason_blocked=NULL, promoted_ts_ms=COALESCE(promoted_ts_ms, ?), last_decision_ts_ms=?
                    WHERE symbol=? AND horizon_s=? AND model_name=?
                    """,
                    (int(ts_ms), int(ts_ms), str(sym), int(h), str(m)),
                )
                promoted += 1
            else:
                con.execute(
                    """
                    UPDATE marketplace_model_state
                    SET tier='candidate', eligible=1, reason_blocked=NULL, last_decision_ts_ms=?
                    WHERE symbol=? AND horizon_s=? AND model_name=?
                    """,
                    (int(ts_ms), str(sym), int(h), str(m)),
                )

    return {"live_set": int(promoted), "blocked_set": int(blocked)}


def _write_allocations(con, *, ts_ms: int, window_days: int) -> Dict[str, Any]:
    # Allocation = equal weight across live buckets, then equal within bucket.
    live = con.execute(
        """
        SELECT s.symbol, s.horizon_s, s.model_name, ms.score
        FROM marketplace_model_state s
        JOIN marketplace_model_score ms
          ON ms.ts_ms=? AND ms.window_days=?
         AND ms.symbol=s.symbol AND ms.horizon_s=s.horizon_s AND ms.model_name=s.model_name
        WHERE s.tier='live' AND s.eligible=1
        ORDER BY ms.score DESC, s.symbol ASC, s.horizon_s ASC, s.model_name ASC
        """,
        (int(ts_ms), int(window_days)),
    ).fetchall()

    if not live:
        return {"alloc_rows": 0}

    # bucket weights: normalize by positive score
    scores = [max(0.0, _safe_float(r[3], 0.0)) for r in live]
    total = float(sum(scores))
    if total <= 1e-12:
        total = float(len(live))
        scores = [1.0 for _ in live]

    for (row, scw) in zip(live, scores):
        sym = str(row[0])
        h = int(row[1])
        m = str(row[2])
        w = float(scw) / float(total)
        con.execute(
            """
            INSERT OR REPLACE INTO marketplace_allocation(ts_ms, symbol, horizon_s, model_name, target_weight, reason_json)
            VALUES (?,?,?,?,?,?)
            """,
            (
                int(ts_ms),
                str(sym),
                int(h),
                str(m),
                float(w),
                json.dumps({"method": "score_proportional", "window_days": int(window_days)}, separators=(",", ":"), sort_keys=True),
            ),
        )

    return {"alloc_rows": int(len(live))}


def _write_training_plan(con, *, ts_ms: int, window_days: int) -> Dict[str, Any]:
    # Deterministic exploit/explore split over (symbol,horizon,model) entries.
    # Exploit: based on live model scores. Explore: top WATCH symbols by current score (or fallback).
    con.execute(
        """
        INSERT OR REPLACE INTO marketplace_training_budget(ts_ms, scope, total_budget_units)
        VALUES (?,?,?)
        """,
        (int(ts_ms), "global", float(TOTAL_BUDGET_UNITS)),
    )

    exploit_budget = float(TOTAL_BUDGET_UNITS) * float(max(0.0, min(1.0, 1.0 - EXPLORE_FRACTION)))
    explore_budget = float(TOTAL_BUDGET_UNITS) - float(exploit_budget)

    live = con.execute(
        """
        SELECT ms.symbol, ms.horizon_s, ms.model_name, ms.score, ms.n
        FROM marketplace_model_state st
        JOIN marketplace_model_score ms
          ON ms.ts_ms=? AND ms.window_days=?
         AND ms.symbol=st.symbol AND ms.horizon_s=st.horizon_s AND ms.model_name=st.model_name
        WHERE st.tier='live' AND st.eligible=1
        ORDER BY ms.score DESC, ms.n DESC, ms.symbol ASC, ms.horizon_s ASC, ms.model_name ASC
        LIMIT 5000
        """,
        (int(ts_ms), int(window_days)),
    ).fetchall()

    live_scores = [max(0.0, _safe_float(r[3], 0.0)) for r in live]
    denom = float(sum(live_scores))
    if denom <= 1e-12:
        denom = float(len(live) or 1)
        live_scores = [1.0 for _ in live]

    priority = 1
    for row, sc in zip(live, live_scores):
        sym, h, m, _, n = row
        w = float(sc) / float(denom)
        units = float(exploit_budget) * float(w)
        con.execute(
            """
            INSERT OR REPLACE INTO marketplace_training_plan(ts_ms, symbol, horizon_s, model_name, budget_units, priority, reason, detail_json)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                int(ts_ms),
                str(sym),
                int(h),
                str(m),
                float(units),
                int(priority),
                "exploit",
                json.dumps({"window_days": int(window_days), "n": int(n or 0)}, separators=(",", ":"), sort_keys=True),
            ),
        )
        priority += 1

    # Explore: pick WATCH symbols by symbols.score (existing universe) and schedule baseline training
    watch = con.execute(
        """
        SELECT symbol
        FROM symbols
        WHERE status IN ('WATCH','COOLDOWN')
        ORDER BY score DESC, updated_ts_ms DESC, symbol ASC
        LIMIT 200
        """
    ).fetchall()

    explore_targets = [str(r[0]) for r in watch or [] if r and r[0]]
    if not explore_targets:
        return {"plan_rows": int(len(live))}

    per = float(explore_budget) / float(len(explore_targets) or 1)
    for sym in explore_targets:
        con.execute(
            """
            INSERT OR REPLACE INTO marketplace_training_plan(ts_ms, symbol, horizon_s, model_name, budget_units, priority, reason, detail_json)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                int(ts_ms),
                str(sym).strip().upper(),
                0,
                "baseline",
                float(per),
                int(priority),
                "explore",
                json.dumps({"window_days": int(window_days)}, separators=(",", ":"), sort_keys=True),
            ),
        )
        priority += 1

    return {"plan_rows": int(priority - 1)}


def main() -> int:
    init_db()

    owner = os.environ.get("JOB_OWNER", os.environ.get("HOSTNAME", "unknown"))
    pid = os.getpid()

    if not acquire_job_lock(str(LOCK_NAME), str(owner), int(pid), ttl_s=int(LOCK_STALE_S)):
        print(json.dumps({"ok": True, "skipped": True, "reason": "lock held"}, sort_keys=True))
        return 0

    con = connect(readonly=False)
    try:
        ts_ms = _now_ms()
        touch_job_lock(str(LOCK_NAME), str(owner), int(pid))

        out: Dict[str, Any] = {"ok": True, "ts_ms": int(ts_ms), "windows": {}, "actions": {}}

        for wd in WINDOW_DAYS:
            model_out, symbol_out = _aggregate(con, window_days=int(wd), asof_ms=int(ts_ms))
            _upsert_scores_and_states(con, ts_ms=int(ts_ms), window_days=int(wd), model_out=model_out, symbol_out=symbol_out)

            tiers = _rank_and_set_model_tiers(con, ts_ms=int(ts_ms), window_days=int(wd))
            alloc = _write_allocations(con, ts_ms=int(ts_ms), window_days=int(wd))
            plan = _write_training_plan(con, ts_ms=int(ts_ms), window_days=int(wd))

            out["windows"][str(wd)] = {
                "models_scored": int(len(model_out)),
                "symbols_scored": int(len(symbol_out)),
                **tiers,
                **alloc,
                **plan,
            }

        # Apply universe updates from the largest window (last in list) for stability
        use_wd = int(WINDOW_DAYS[-1]) if WINDOW_DAYS else 60
        out["actions"]["universe_update"] = _set_universe_from_symbol_scores(con, ts_ms=int(ts_ms), window_days=int(use_wd))

        con.commit()
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    except Exception as e:
        try:
            con.rollback()
        except Exception:
            pass
        print(json.dumps({"ok": False, "error": str(e)}, sort_keys=True))
        raise
    finally:
        try:
            con.close()
        except Exception:
            pass
        try:
            release_job_lock(str(LOCK_NAME), str(owner), int(pid))
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
