"""
Advanced Read-Only API Endpoints
Moved from dashboard_server to enforce layer isolation.
"""

import json
import time

from engine.api.internal_access import db_connect


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def _table_exists(con, name: str) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(name),),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _broker_fills_table(con) -> str:
    if _table_exists(con, "broker_fills_v2"):
        return "broker_fills_v2"
    return "broker_fills"


# --------------------------------------------------
# MODEL DIAGNOSTICS
# --------------------------------------------------

def get_model_diagnostics():
    con = db_connect()
    try:
        out = {}

        try:
            rows = con.execute(
                """
                SELECT symbol, horizon_s, regime, n, mean_impact_z
                FROM model_stats_regime
                ORDER BY symbol, horizon_s, regime
                """
            ).fetchall()
        except Exception:
            rows = []

        priors = {}
        for sym, h, reg, n, mean_z in rows:
            priors.setdefault(f"{sym}:{h}", []).append({
                "regime": reg,
                "n": int(n),
                "mean_z": float(mean_z),
            })
        out["regime_priors"] = priors

        try:
            rows = con.execute(
                """
                SELECT symbol, horizon_s, n, mean_impact_z
                FROM model_stats
                ORDER BY symbol, horizon_s
                """
            ).fetchall()
        except Exception:
            rows = []

        out["global_priors"] = [
            {"symbol": r[0], "horizon_s": r[1], "n": int(r[2]), "mean_z": float(r[3])}
            for r in rows
        ]

        try:
            rows = con.execute(
                """
                SELECT target_symbol, driver_symbol, horizon_s, n, beta
                FROM spillover_beta
                ORDER BY target_symbol, horizon_s, n DESC
                """
            ).fetchall()
        except Exception:
            rows = []

        spill = {}
        for tgt, drv, h, n, beta in rows:
            spill.setdefault(f"{tgt}:{h}", []).append({
                "driver": drv,
                "n": int(n),
                "beta": float(beta),
            })
        out["spillovers"] = spill

        return out
    finally:
        con.close()


# --------------------------------------------------
# TEMPORAL MODELS
# --------------------------------------------------

def get_temporal_models(limit: int = 20):
    limit = max(1, min(5000, int(limit or 20)))
    con = db_connect()
    try:
        try:
            rows = con.execute(
                """
                SELECT model_name, window, input_dim, ts_ms, metrics_json,
                       LENGTH(weights) as weights_bytes
                FROM temporal_models
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            try:
                mj = json.loads(r[4] or "{}")
            except Exception:
                mj = {}

            out.append({
                "model_name": str(r[0] or ""),
                "window": int(r[1] or 0),
                "input_dim": int(r[2] or 0),
                "ts_ms": int(r[3] or 0),
                "weights_bytes": int(r[5] or 0),
                "metrics": mj,
            })

        return {"ok": True, "rows": out}
    finally:
        con.close()


# --------------------------------------------------
# PORTFOLIO BACKTEST READ
# --------------------------------------------------

def get_latest_portfolio_backtest():
    con = db_connect()
    try:
        row = con.execute(
            """
            SELECT id, ts_ms, start_ts_ms, end_ts_ms, metrics_json
            FROM portfolio_bt_runs
            ORDER BY ts_ms DESC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            return {"ok": False, "error": "no portfolio backtest runs"}

        run_id, ts_ms, start_ts_ms, end_ts_ms, metrics_json = row

        try:
            metrics = json.loads(metrics_json or "{}")
        except Exception:
            metrics = {}

        pts = con.execute(
            """
            SELECT ts_ms, ret, equity, drawdown, detail_json
            FROM portfolio_bt_points
            WHERE run_id = ?
            ORDER BY ts_ms ASC
            """,
            (int(run_id),),
        ).fetchall()

        points = []
        for r in pts:
            try:
                detail = json.loads(r[4] or "{}")
            except Exception:
                detail = {}
            points.append({
                "ts_ms": int(r[0]),
                "ret": float(r[1]),
                "equity": float(r[2]),
                "drawdown": float(r[3]),
                "detail": detail,
            })

        return {
            "ok": True,
            "run": {
                "id": int(run_id),
                "ts_ms": int(ts_ms),
                "start_ts_ms": int(start_ts_ms),
                "end_ts_ms": int(end_ts_ms),
                "metrics": metrics,
                "points": points,
            },
        }
    finally:
        con.close()


# --------------------------------------------------
# PORTFOLIO SNAPSHOT (dashboard)
# --------------------------------------------------

def get_portfolio_snapshot(
    limit_state: int = 200,
    intents_window_ms: int = 2500,
    intents_max_rows: int = 5000,
):
    """
    Dashboard contract: never return ok=False.
    Returns ok=True with empty structures if portfolio has not been produced yet.
    """
    con = db_connect()
    try:
        # If portfolio tables don't exist yet, return empty but ok.
        if (not _table_exists(con, "portfolio_state")) or (not _table_exists(con, "portfolio_orders")):
            return {
                "ok": True,
                "meta": {"ready": False, "reason": "portfolio_tables_missing"},
                "state": [],
                "orders": [],
            }

        # Meta (best-effort)
        meta_rows = []
        if _table_exists(con, "portfolio_meta"):
            try:
                meta_rows = con.execute(
                    "SELECT key, value FROM portfolio_meta ORDER BY key ASC"
                ).fetchall() or []
            except Exception:
                meta_rows = []

        meta = {}
        for k, v in (meta_rows or []):
            ks = str(k or "").strip()
            if not ks:
                continue
            meta[ks] = str(v) if v is not None else ""

        # State rows
        try:
            st_rows = con.execute(
                """
                SELECT symbol, side, weight, opened_ts_ms, updated_ts_ms, source_alert_id, explain_json
                FROM portfolio_state
                ORDER BY ABS(weight) DESC, updated_ts_ms DESC
                LIMIT ?
                """,
                (int(limit_state),),
            ).fetchall() or []
        except Exception:
            st_rows = []

        state = []
        for r in st_rows:
            try:
                symbol, side, weight, opened_ts_ms, updated_ts_ms, source_alert_id, explain_json = r
            except Exception:
                continue
            try:
                ex = json.loads(explain_json or "{}") if explain_json else {}
            except Exception:
                ex = {}
            state.append(
                {
                    "symbol": str(symbol or ""),
                    "side": str(side or ""),
                    "weight": float(weight or 0.0),
                    "opened_ts_ms": int(opened_ts_ms or 0),
                    "updated_ts_ms": int(updated_ts_ms or 0),
                    "source_alert_id": (int(source_alert_id) if source_alert_id is not None else None),
                    "explain": (ex if isinstance(ex, dict) else {}),
                }
            )

        # Orders/intents (latest batch)
        try:
            from engine.strategy.portfolio_execution_intents import load_latest_execution_intents

            intents_res = load_latest_execution_intents(
                con,
                window_ms=int(intents_window_ms),
                max_rows=int(intents_max_rows),
            )
        except Exception:
            intents_res = {"ok": True, "batch_id": None, "batch_ts_ms": None, "intents": []}

        orders = []
        if isinstance(intents_res, dict):
            for it in (intents_res.get("intents") or []):
                if isinstance(it, dict):
                    orders.append(it)

        return {
            "ok": True,
            "meta": {
                "ready": True,
                "meta": meta,
                "orders_batch_id": (intents_res.get("batch_id") if isinstance(intents_res, dict) else None),
                "orders_batch_ts_ms": (intents_res.get("batch_ts_ms") if isinstance(intents_res, dict) else None),
            },
            "state": state,
            "orders": orders,
        }
    finally:
        con.close()


# --------------------------------------------------
# PORTFOLIO SNAPSHOT (dashboard)
# --------------------------------------------------

def get_portfolio_snapshot(
    limit_state: int = 200,
    intents_window_ms: int = 2500,
    intents_max_rows: int = 5000,
):
    con = db_connect()
    try:
        if (not _table_exists(con, "portfolio_state")) or (not _table_exists(con, "portfolio_orders")):
            return {
                "ok": True,
                "meta": {"ready": False, "reason": "portfolio_tables_missing"},
                "state": [],
                "orders": [],
            }

        # State
        try:
            st_rows = con.execute(
                """
                SELECT symbol, side, weight, opened_ts_ms, updated_ts_ms, source_alert_id, explain_json
                FROM portfolio_state
                ORDER BY ABS(weight) DESC, updated_ts_ms DESC
                LIMIT ?
                """,
                (int(limit_state),),
            ).fetchall() or []
        except Exception:
            st_rows = []

        state = []
        for r in st_rows:
            try:
                symbol, side, weight, opened_ts_ms, updated_ts_ms, source_alert_id, explain_json = r
            except Exception:
                continue
            try:
                ex = json.loads(explain_json or "{}") if explain_json else {}
            except Exception:
                ex = {}
            state.append(
                {
                    "symbol": str(symbol or ""),
                    "side": str(side or ""),
                    "weight": float(weight or 0.0),
                    "opened_ts_ms": int(opened_ts_ms or 0),
                    "updated_ts_ms": int(updated_ts_ms or 0),
                    "source_alert_id": (int(source_alert_id) if source_alert_id is not None else None),
                    "explain": ex if isinstance(ex, dict) else {},
                }
            )

        # Orders
        try:
            from engine.strategy.portfolio_execution_intents import load_latest_execution_intents
            intents_res = load_latest_execution_intents(
                con,
                window_ms=int(intents_window_ms),
                max_rows=int(intents_max_rows),
            )
        except Exception:
            intents_res = {"ok": True, "batch_id": None, "batch_ts_ms": None, "intents": []}

        orders = intents_res.get("intents") if isinstance(intents_res, dict) else []

        return {
            "ok": True,
            "meta": {
                "ready": True,
                "orders_batch_id": intents_res.get("batch_id") if isinstance(intents_res, dict) else None,
                "orders_batch_ts_ms": intents_res.get("batch_ts_ms") if isinstance(intents_res, dict) else None,
            },
            "state": state,
            "orders": orders or [],
        }
    finally:
        con.close()


# --------------------------------------------------
# EXECUTION METRICS
# --------------------------------------------------

def get_execution_metrics_rolling():
    con = db_connect()
    try:
        fills_table = _broker_fills_table(con)

        now_ms = int(time.time() * 1000)
        day_ms = 24 * 60 * 60 * 1000
        week_ms = 7 * day_ms

        def _q(since_ms):
            try:
                return con.execute(
                    f"""
                    SELECT
                      COUNT(*)        AS n_fills,
                      SUM(slippage)   AS total_slippage,
                      SUM(fees)       AS total_fees,
                      SUM(total_cost) AS total_cost,
                      AVG(slippage)   AS avg_slippage
                    FROM {fills_table}
                    WHERE ts_ms >= ?
                    """,
                    (int(since_ms),),
                ).fetchone()
            except Exception:
                return None

        r_24h = _q(now_ms - day_ms)
        r_7d  = _q(now_ms - week_ms)

        def _row(r):
            return {
                "n_fills": int(r[0] or 0) if r else 0,
                "total_slippage": float(r[1] or 0.0) if r else 0.0,
                "total_fees": float(r[2] or 0.0) if r else 0.0,
                "total_cost": float(r[3] or 0.0) if r else 0.0,
                "avg_slippage": float(r[4] or 0.0) if r else 0.0,
            }

        return {"ok": True, "last_24h": _row(r_24h), "last_7d": _row(r_7d)}
    finally:
        con.close()


def get_execution_metrics_by_symbol(limit: int = 50):
    limit = max(1, min(500, int(limit or 50)))
    con = db_connect()
    try:
        fills_table = _broker_fills_table(con)

        try:
            rows = con.execute(
                f"""
                SELECT
                  symbol,
                  COUNT(*)        AS n_fills,
                  SUM(slippage)   AS total_slippage,
                  SUM(fees)       AS total_fees,
                  SUM(total_cost) AS total_cost,
                  AVG(slippage)   AS avg_slippage
                FROM {fills_table}
                GROUP BY symbol
                ORDER BY total_cost DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except Exception:
            rows = []

        return {
            "ok": True,
            "symbols": [
                {
                    "symbol": r[0],
                    "n_fills": int(r[1] or 0),
                    "total_slippage": float(r[2] or 0.0),
                    "total_fees": float(r[3] or 0.0),
                    "total_cost": float(r[4] or 0.0),
                    "avg_slippage": float(r[5] or 0.0),
                }
                for r in rows
            ],
        }
    finally:
        con.close()


def get_execution_cost_by_confidence():
    con = db_connect()
    try:
        fills_table = _broker_fills_table(con)

        try:
            rows = con.execute(
                f"""
                SELECT
                  CAST(confidence * 10 AS INTEGER) AS bucket,
                  COUNT(*)        AS n_fills,
                  SUM(total_cost) AS total_cost,
                  AVG(total_cost) AS avg_cost
                FROM {fills_table}
                WHERE confidence IS NOT NULL
                GROUP BY bucket
                ORDER BY bucket ASC
                """
            ).fetchall()
        except Exception:
            rows = []

        buckets = []
        for b, n, tc, ac in rows:
            lo = max(0.0, min(0.9, (int(b) or 0) / 10.0))
            hi = lo + 0.1
            buckets.append({
                "conf_lo": lo,
                "conf_hi": hi,
                "n_fills": int(n or 0),
                "total_cost": float(tc or 0.0),
                "avg_cost": float(ac or 0.0),
            })

        return {"ok": True, "buckets": buckets}
    finally:
        con.close()


# --------------------------------------------------
# SOCIAL READS
# --------------------------------------------------

def get_social_features(symbol: str, limit: int = 200):
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {"ok": True, "rows": []}

    limit = max(1, min(5000, int(limit or 200)))

    con = db_connect()
    try:
        if not _table_exists(con, "social_features"):
            return {"ok": True, "rows": []}

        try:
            rows = con.execute(
                """
                SELECT
                  bucket_ts_ms,
                  bucket_sec,

                  mention_count,
                  unique_authors,
                  new_author_ratio,
                  engagement_now,

                  sentiment_mean,
                  sentiment_dispersion,

                  mention_rate_z,
                  bot_likelihood_mean,
                  promo_likelihood_mean,
                  manip_risk,
                  attention_shock,

                  cross_platform_confirm
                FROM social_features
                WHERE symbol = ?
                ORDER BY bucket_ts_ms DESC
                LIMIT ?
                """,
                (sym, int(limit)),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            try:
                out.append({
                    "bucket_ts_ms": int(r[0] or 0),
                    "bucket_sec": int(r[1] or 0),

                    "mention_count": int(r[2] or 0),
                    "unique_authors": int(r[3] or 0),
                    "new_author_ratio": float(r[4] or 0.0),
                    "engagement_now": float(r[5] or 0.0),

                    "sentiment_mean": float(r[6] or 0.0),
                    "sentiment_dispersion": float(r[7] or 0.0),

                    "mention_rate_z": float(r[8] or 0.0),
                    "bot_likelihood_mean": float(r[9] or 0.0),
                    "promo_likelihood_mean": float(r[10] or 0.0),
                    "manip_risk": float(r[11] or 0.0),
                    "attention_shock": float(r[12] or 0.0),

                    "cross_platform_confirm": float(r[13] or 0.0),
                })
            except Exception:
                continue

        return {"ok": True, "symbol": sym, "rows": out}
    finally:
        con.close()


def get_social_regimes(symbol: str, limit: int = 200):
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {"ok": True, "rows": []}

    limit = max(1, min(5000, int(limit or 200)))

    con = db_connect()
    try:
        if not _table_exists(con, "social_regimes"):
            return {"ok": True, "rows": []}

        try:
            rows = con.execute(
                """
                SELECT
                  bucket_ts_ms,
                  bucket_sec,
                  regime,
                  regime_conf,
                  features_json
                FROM social_regimes
                WHERE symbol = ?
                ORDER BY bucket_ts_ms DESC
                LIMIT ?
                """,
                (sym, int(limit)),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            try:
                out.append({
                    "bucket_ts_ms": int(r[0] or 0),
                    "bucket_sec": int(r[1] or 0),
                    "regime": str(r[2] or ""),
                    "regime_conf": float(r[3] or 0.0),
                    "features": (json.loads(r[4]) if (r[4] or "").strip() else None),
                })
            except Exception:
                continue

        return {"ok": True, "symbol": sym, "rows": out}
    finally:
        con.close()


def get_social_blocks(limit: int = 200):
    limit = max(1, min(2000, int(limit or 200)))

    con = db_connect()
    try:
        table = None
        for t in ("decision_log", "decisions", "trade_decisions"):
            if _table_exists(con, t):
                table = t
                break

        if not table:
            return {"ok": True, "rows": []}

        rows = []
        try:
            rows = con.execute(
                f"""
                SELECT ts_ms, symbol, reason_json
                FROM {table}
                WHERE json_extract(reason_json, '$.social_gate_block') = 1
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except Exception:
            try:
                rows = con.execute(
                    f"""
                    SELECT ts_ms, symbol, reason_json
                    FROM {table}
                    ORDER BY ts_ms DESC
                    LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
            except Exception:
                rows = []

        out = []
        for r in rows or []:
            try:
                out.append({
                    "ts_ms": int(r[0] or 0),
                    "symbol": str(r[1] or ""),
                    "reason": (json.loads(r[2]) if (r[2] or "").strip() else {}),
                })
            except Exception:
                continue

        return {"ok": True, "table": table, "rows": out}
    finally:
        con.close()

# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------

def get_validation_rows():
    try:
        from engine.validation import get_validation as _get_validation
        return {"ok": True, "rows": _get_validation()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ----------------------------------------------------------------------
# Shadow Capital Allocation
# ----------------------------------------------------------------------

def get_shadow_capital_scores(limit: int = 50, regime: str = "global"):
    try:
        from engine.runtime.shadow_capital_allocator import (
            get_shadow_capital_scores as _impl,
        )
        return _impl(limit=limit, regime=regime)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_shadow_capital_scores(window_s: int = 86400, regime: str = "global"):
    try:
        from engine.runtime.shadow_capital_allocator import (
            compute_and_persist_shadow_capital_scores as _run,
        )
        return _run(window_s=window_s, regime=regime)
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ----------------------------------------------------------------------
# Size Policy
# ----------------------------------------------------------------------

def get_size_policy():
    from engine.api.internal_access import db_connect as _db_connect

    con = _db_connect()
    try:
        try:
            r = con.execute(
                """
                SELECT id, ts_ms, lookback_days, buckets, method, params_json, metrics_json
                FROM size_policy
                ORDER BY ts_ms DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            r = None

        if not r:
            return {"ok": True, "policy": None, "points": []}

        pid, ts_ms, lookback_days, buckets, method, params_json, metrics_json = r

        try:
            params = json.loads(params_json or "{}")
        except Exception:
            params = {}

        try:
            metrics = json.loads(metrics_json or "{}")
        except Exception:
            metrics = {}

        try:
            pts = con.execute(
                """
                SELECT bucket_idx, conf_lo, conf_hi, n, mean_net_ret, std_net_ret, factor
                FROM size_policy_points
                WHERE policy_id=?
                ORDER BY bucket_idx ASC
                """,
                (int(pid),),
            ).fetchall()
        except Exception:
            pts = []

        points = []
        for bi, clo, chi, n, mnr, sdr, f in pts or []:
            points.append({
                "bucket_idx": int(bi),
                "conf_lo": float(clo),
                "conf_hi": float(chi),
                "n": int(n),
                "mean_net_ret": float(mnr),
                "std_net_ret": float(sdr),
                "factor": float(f),
            })

        return {
            "ok": True,
            "policy": {
                "id": int(pid),
                "ts_ms": int(ts_ms),
                "lookback_days": int(lookback_days),
                "buckets": int(buckets),
                "method": str(method),
                "params": params,
                "metrics": metrics,
            },
            "points": points,
        }
    finally:
        con.close()
