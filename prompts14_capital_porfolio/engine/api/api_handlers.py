# FILE: engine/api/api_handlers.py
# FIND (replace entire file content)
import json
import time
import threading
from urllib.parse import parse_qs

from engine.runtime.storage import connect as _db_connect
from dev_core.learning import learn_relevance_stats

from health_checks import get_health_snapshot
from ops.alerts_service import get_alerts
from execution_metrics import (
    get_execution_metrics,
    get_execution_metrics_rolling,
)

from engine.strategy.pipeline_runner import (
    LAST_AUTO_PIPELINE_TS,
    LAST_AUTO_CHALLENGER_TS,
    LAST_AUTO_SIZE_POLICY_TS,
)

from engine.runtime.jobs_manager import get_job_log, get_job_history
from engine.runtime.lifecycle import snapshot as lifecycle_snapshot
from engine.runtime.system_state import compute_system_state

from dashboard_config import (
    ENABLE_RELEVANCE_STATS,
    RELEVANCE_STATS_CACHE_TTL_S,
    RELEVANCE_STATS_TIMEOUT_S,
    AUTO_PIPELINE,
    AUTO_CHALLENGER,
    AUTO_SIZE_POLICY,
)

# -------------------------------------------------
# Relevance stats (cached + timeout guarded)
# -------------------------------------------------

_relevance_cache = {"ts": 0.0, "value": None}


def _compute_relevance_stats_with_timeout(timeout_s: float):
    result = {}
    error = {}

    def _runner():
        try:
            result["value"] = learn_relevance_stats()
        except Exception as e:
            error["error"] = str(e)

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout_s)

    if t.is_alive():
        raise TimeoutError(f"learn_relevance_stats timed out after {timeout_s}s")

    if "error" in error:
        raise RuntimeError(error["error"])

    return result.get("value")


def get_relevance_stats():
    if not ENABLE_RELEVANCE_STATS:
        return {"ok": False, "error": "relevance stats disabled (ENABLE_RELEVANCE_STATS=0)"}

    now = time.time()

    if (
        _relevance_cache["value"] is not None
        and (now - _relevance_cache["ts"]) < RELEVANCE_STATS_CACHE_TTL_S
    ):
        return {
            "ok": True,
            "cached": True,
            "stats": _relevance_cache["value"],
        }

    try:
        stats = _compute_relevance_stats_with_timeout(
            RELEVANCE_STATS_TIMEOUT_S
        )
        _relevance_cache["value"] = stats
        _relevance_cache["ts"] = now
        return {"ok": True, "cached": False, "stats": stats}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -------------------------------------------------
# Helpers
# -------------------------------------------------

def _qs(parsed):
    try:
        q = parse_qs(parsed.query or "")
        return {k: v[0] for k, v in q.items()}
    except Exception:
        return {}


def _deny_if_shutdown():
    try:
        snap = lifecycle_snapshot() or {}
        if str(snap.get("state") or "").upper() == "SHUTDOWN":
            return {"ok": False, "error": "server_shutting_down"}
    except Exception:
        pass
    return None
    try:
        q = getattr(parsed, "query", "") or ""
        d = parse_qs(q, keep_blank_values=True)
        return {k: (v[0] if isinstance(v, list) and v else "") for k, v in d.items()}
    except Exception:
        return {}


def _normalize_explain_json(val) -> str:
    if val is None:
        return "{}"
    try:
        if isinstance(val, (bytes, bytearray)):
            val = val.decode("utf-8", errors="replace")
    except Exception:
        pass

    s = str(val).strip()
    if not s:
        return "{}"

    try:
        json.loads(s)
        return s
    except Exception:
        return json.dumps({"raw": s})


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


# -------------------------------------------------
# System / health
# -------------------------------------------------

def api_get_health(_parsed, _ctx):
    return get_health_snapshot()


def api_get_kill_switches(_parsed, _ctx):
    # basic static response, may be overridden by ENV for expiry or toggles
    resp = {
        "ok": True,
        "enabled": False,
        "kill_switches": {},
        "meta": {
            "schedulers": {
                "auto_pipeline": {
                    "enabled": bool(AUTO_PIPELINE),
                    "reason": None if AUTO_PIPELINE else "AUTO_PIPELINE=0",
                    "last_run": LAST_AUTO_PIPELINE_TS,
                },
                "auto_challenger": {
                    "enabled": bool(AUTO_CHALLENGER),
                    "reason": None if AUTO_CHALLENGER else "AUTO_CHALLENGER=0",
                    "last_run": LAST_AUTO_CHALLENGER_TS,
                },
                "auto_size_policy": {
                    "enabled": bool(AUTO_SIZE_POLICY),
                    "reason": None if AUTO_SIZE_POLICY else "AUTO_SIZE_POLICY=0",
                    "last_run": LAST_AUTO_SIZE_POLICY_TS,
                },
            }
        },
    }

    # support simple ENV toggle (e.g. for testing)
    if os.environ.get("KILL_SWITCH", "").strip().lower() in ("1", "true", "on"):
        resp["enabled"] = True

    # auto-expire based on TS environment variable
    expire = os.environ.get("KILL_SWITCH_EXPIRE_TS", "").strip()
    if expire:
        try:
            if time.time() > float(expire):
                resp["enabled"] = False
            else:
                resp["enabled"] = True
        except Exception:
            pass

    return resp


def api_get_system_state(_parsed, _ctx):
    health = get_health_snapshot()

    try:
        JOBS = _ctx.get("JOBS")
        jobs = JOBS.list_jobs() if JOBS else []
    except Exception:
        jobs = []

    try:
        kill_switches = api_get_kill_switches(None, _ctx) or {}
    except Exception:
        kill_switches = {}

    state = compute_system_state(
        health=health,
        jobs=jobs,
        kill_switches=kill_switches,
    )
    state["lifecycle"] = lifecycle_snapshot()
    return state


# -------------------------------------------------
# Jobs
# -------------------------------------------------

def api_get_jobs(_parsed, _ctx):
    JOBS = _ctx["JOBS"]
    return {"ok": True, "jobs": JOBS.list_jobs()}


def api_post_job_start(_parsed, body, _ctx):
    JOBS = _ctx.get("JOBS")
    if JOBS is None:
        return {"ok": False, "error": "missing_ctx:JOBS"}

    name = ""
    if isinstance(body, dict):
        name = str(body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing_name"}

    allowed = _ctx.get("ALLOWED_JOBS") or {}
    if allowed and name not in allowed:
        return {"ok": False, "error": f"job_not_allowed:{name}"}

    return JOBS.start(name)


def api_post_job_stop(_parsed, body, _ctx):
    JOBS = _ctx.get("JOBS")
    if JOBS is None:
        return {"ok": False, "error": "missing_ctx:JOBS"}

    name = ""
    if isinstance(body, dict):
        name = str(body.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing_name"}

    allowed = _ctx.get("ALLOWED_JOBS") or {}
    if allowed and name not in allowed:
        return {"ok": False, "error": f"job_not_allowed:{name}"}

    return JOBS.stop(name)


def api_post_pipeline_run(_parsed, _body, _ctx):
    orchestrator = _ctx.get("ORCHESTRATOR")
    if orchestrator is None:
        return {"ok": False, "error": "missing_ctx:ORCHESTRATOR"}
    return orchestrator.run_pipeline()


def api_get_job_log(parsed, _ctx):
    try:
        qs = _qs(parsed)
        name = (qs.get("name", "") or "").strip()
        tail = int((qs.get("tail", "200") or "200"))
    except Exception:
        name = ""
        tail = 200

    tail = max(1, min(5000, int(tail)))
    return {"ok": True, "log": get_job_log(name, tail)}


def api_get_job_history(parsed, _ctx):
    try:
        qs = _qs(parsed)
        name = (qs.get("name", "") or "").strip()
        limit = int((qs.get("limit", "200") or "200"))
    except Exception:
        name = ""
        limit = 200

    limit = max(1, min(5000, int(limit)))
    return {"ok": True, "rows": get_job_history(name, limit)}


# -------------------------------------------------
# Alerts
# -------------------------------------------------

def api_get_alerts(_parsed, _ctx):
    return {"ok": True, "rows": get_alerts()}


def api_get_validation(_parsed, _ctx):
    try:
        from engine.strategy.validation import get_validation
        return {"ok": True, "rows": get_validation()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# -------------------------------------------------
# Execution metrics
# -------------------------------------------------

def api_get_execution_metrics(_parsed, _ctx):
    return get_execution_metrics()


def api_get_execution_metrics_rolling(_parsed, _ctx):
    return get_execution_metrics_rolling()


def api_get_execution_metrics_by_symbol(parsed, _ctx):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    limit = max(1, min(500, int(limit)))

    con = _db_connect()
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


def api_get_execution_cost_by_confidence(_parsed, _ctx):
    con = _db_connect()
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


# -------------------------------------------------
# Model diagnostics / registry / eval
# -------------------------------------------------

def api_get_model_diagnostics(_parsed, _ctx):
    con = _db_connect()
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

        return {"ok": True, "data": out}
    finally:
        con.close()


def api_get_model_registry(parsed, _ctx):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    limit = max(1, min(500, int(limit)))

    con = _db_connect()
    try:
        try:
            ch = con.execute(
                """
                SELECT model_kind, model_ts_ms, metrics_json, created_ts_ms, note
                FROM model_registry
                WHERE model_name='embed_regressor' AND stage='champion'
                ORDER BY created_ts_ms DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            ch = None

        try:
            cl = con.execute(
                """
                SELECT model_kind, model_ts_ms, metrics_json, created_ts_ms, note
                FROM model_registry
                WHERE model_name='embed_regressor' AND stage='challenger'
                ORDER BY created_ts_ms DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            cl = None

        try:
            rows = con.execute(
                """
                SELECT model_kind, model_ts_ms, stage, metrics_json, created_ts_ms, note
                FROM model_registry
                WHERE model_name='embed_regressor'
                ORDER BY created_ts_ms DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except Exception:
            rows = []

        def _row(r):
            return {
                "model_kind": r[0],
                "model_ts_ms": int(r[1]),
                "stage": r[2],
                "metrics": json.loads(r[3] or "{}"),
                "created_ts_ms": int(r[4]),
                "note": r[5],
            }

        return {
            "ok": True,
            "champion": _row(ch) if ch else None,
            "challenger": _row(cl) if cl else None,
            "history": [_row(r) for r in rows],
        }
    finally:
        con.close()


def api_get_embed_model_eval(parsed, _ctx):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "500") or "500")
    limit = max(1, min(5000, int(limit)))

    con = _db_connect()
    try:
        try:
            rows = con.execute(
                """
                SELECT key_type, key, horizon_s, model_kind, ts_ms,
                       n_train, n_eval, rmse, spearman, directional_acc
                FROM embed_model_eval
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            out.append({
                "key_type": str(r[0] or ""),
                "key": str(r[1] or ""),
                "horizon_s": int(r[2] or 0),
                "model_kind": str(r[3] or ""),
                "ts_ms": int(r[4] or 0),
                "n_train": int(r[5] or 0),
                "n_eval": int(r[6] or 0),
                "rmse": float(r[7] or 0.0),
                "spearman": float(r[8] or 0.0),
                "directional_acc": float(r[9] or 0.0),
            })
        return {"ok": True, "rows": out}
    finally:
        con.close()


def api_get_embed_conf_calib(parsed, _ctx):
    qs = _qs(parsed)
    horizon_s = int(qs.get("horizon_s", "0") or "0")
    model_kind = str(qs.get("model_kind", "") or "")
    limit = int(qs.get("limit", "200") or "200")

    limit = max(2, min(5000, int(limit or 200)))
    hs = int(horizon_s or 0)
    mk = str(model_kind or "").strip().lower()
    if mk not in ("ridge", "mlp"):
        mk = "ridge"

    con = _db_connect()
    try:
        try:
            row = con.execute(
                """
                SELECT ts_ms, conf_k, n_points, x_json, y_json
                FROM embed_conf_calib
                WHERE horizon_s=? AND model_kind=?
                """,
                (int(hs), str(mk)),
            ).fetchone()
        except Exception:
            row = None

        if not row:
            return {"ok": True, "horizon_s": hs, "model_kind": mk, "curve": None}

        ts_ms, conf_k, n_points, xj, yj = row

        try:
            xs = [float(x) for x in json.loads(xj or "[]")]
            ys = [float(y) for y in json.loads(yj or "[]")]
        except Exception:
            xs, ys = [], []

        if len(xs) > limit and len(xs) == len(ys):
            xs = xs[-limit:]
            ys = ys[-limit:]

        curve = [{"x": float(xs[i]), "y": float(ys[i])} for i in range(min(len(xs), len(ys)))]

        return {
            "ok": True,
            "horizon_s": int(hs),
            "model_kind": str(mk),
            "ts_ms": int(ts_ms or 0),
            "conf_k": float(conf_k or 0.0),
            "n_points": int(n_points or len(curve)),
            "curve": curve,
        }
    finally:
        con.close()


def api_get_temporal_eval(parsed, _ctx):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    limit = max(1, min(5000, int(limit)))

    con = _db_connect()
    try:
        try:
            rows = con.execute(
                """
                SELECT horizon_s, n, rmse, directional_acc, ts_ms
                FROM temporal_eval
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        except Exception:
            rows = []

        out = []
        for r in rows or []:
            out.append({
                "horizon_s": int(r[0] or 0),
                "n": int(r[1] or 0),
                "rmse": float(r[2] or 0.0),
                "directional_acc": float(r[3] or 0.0),
                "ts_ms": int(r[4] or 0),
            })

        return {"ok": True, "rows": out}
    finally:
        con.close()


def api_get_temporal_models(parsed, _ctx):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "20") or "20")
    limit = max(1, min(5000, int(limit)))

    con = _db_connect()
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


def api_get_latest_portfolio_backtest(_parsed, _ctx):
    con = _db_connect()
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


# -------------------------------------------------
# Social endpoints
# -------------------------------------------------

def api_get_social_features(parsed, _ctx):
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    limit = max(1, min(5000, int(limit)))

    sym = str(symbol or "").upper().strip()
    con = _db_connect()
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


def api_get_social_regimes(parsed, _ctx):
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    limit = max(1, min(5000, int(limit)))

    sym = str(symbol or "").upper().strip()
    con = _db_connect()
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


def api_get_social_blocks(parsed, _ctx):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "200") or "200")
    limit = max(1, min(2000, int(limit)))

    con = _db_connect()
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


# -------------------------------------------------
# Confidence mass
# -------------------------------------------------

def api_get_confidence_mass(_parsed, _ctx):
    con = _db_connect()
    try:
        try:
            rows = con.execute(
                """
                SELECT confidence
                FROM predictions
                ORDER BY ts_ms DESC
                LIMIT 2000
                """
            ).fetchall()
        except Exception:
            rows = []

        vals = []
        for r in rows:
            try:
                vals.append(float(r[0]))
            except Exception:
                pass

        bins = [0] * 10
        for v in vals:
            v = max(0.0, min(1.0, float(v)))
            idx = int(min(9, max(0, int(v * 10.0))))
            bins[idx] += 1

        return {
            "ok": True,
            "n": int(len(vals)),
            "bins": [
                {"lo": i / 10.0, "hi": (i + 1) / 10.0, "count": int(bins[i])}
                for i in range(10)
            ],
        }
    finally:
        con.close()


# -------------------------------------------------
# Promotion rollback
# -------------------------------------------------

def _rollback_champion():
    from dev_core.model_registry import rollback_champion as _rb
    from dev_core.promotion_audit import audit as _audit

    ch_before = None
    try:
        from dev_core.model_registry import get_stage_latest as _get
        ch_before = _get("embed_regressor", "champion")
    except Exception:
        ch_before = None

    ch_after = _rb("embed_regressor")
    if not ch_after:
        return {"ok": False, "error": "no retired model available to rollback to"}

    _audit(
        actor="manual",
        action="rollback",
        model_name="embed_regressor",
        from_kind=(ch_before.get("model_kind") if ch_before else None),
        from_ts_ms=(ch_before.get("model_ts_ms") if ch_before else None),
        to_kind=ch_after.get("model_kind"),
        to_ts_ms=ch_after.get("model_ts_ms"),
        reason={"note": "api rollback"},
    )
    return {"ok": True, "champion": ch_after}


def api_post_rollback(_parsed, _body, _ctx):
    try:
        return _rollback_champion()
    except Exception as e:
        return {"ok": False, "error": str(e)}
