"""
Read-only API layer.

All DB reads previously in dashboard_server.py now live here.
No runtime orchestration.
No supervisor logic.
Pure read-only DB queries.
"""

import json
import time
from engine.dev_core.storage import connect as _db_connect


def _table_exists(con, name: str) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(name),),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


# ------------------------------
# ALERTS
# ------------------------------

def get_alerts():
    con = _db_connect()
    try:
        rows = con.execute("""
            SELECT
              id, ts_ms, severity, symbol, horizon_s,
              expected_z, confidence, event_title, rule_id, explain_json
            FROM alerts
            ORDER BY ts_ms DESC
            LIMIT 50
        """).fetchall()

        return {
            "ok": True,
            "rows": [
                {
                    "id": r[0],
                    "ts_ms": r[1],
                    "severity": r[2],
                    "symbol": r[3],
                    "horizon_s": r[4],
                    "expected_z": r[5],
                    "confidence": r[6],
                    "event_title": r[7],
                    "rule_id": r[8],
                    "explain_json": r[9] or "{}",
                }
                for r in rows
            ],
        }
    finally:
        con.close()


# ------------------------------
# EXECUTION METRICS
# ------------------------------

def get_execution_metrics():
    con = _db_connect()
    try:
        table = "broker_fills_v2" if _table_exists(con, "broker_fills_v2") else "broker_fills"

        row = con.execute(
            f"""
            SELECT
              COUNT(*),
              SUM(slippage),
              SUM(fees),
              SUM(total_cost),
              AVG(slippage)
            FROM {table}
            """
        ).fetchone()

        return {
            "ok": True,
            "n_fills": int(row[0] or 0),
            "total_slippage": float(row[1] or 0.0),
            "total_fees": float(row[2] or 0.0),
            "total_cost": float(row[3] or 0.0),
            "avg_slippage": float(row[4] or 0.0),
        }
    finally:
        con.close()


# ------------------------------
# MODEL REGISTRY
# ------------------------------

def get_model_registry(limit: int = 50):
    limit = max(1, min(500, int(limit or 50)))

    con = _db_connect()
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

        out = []
        for r in rows:
            try:
                metrics = json.loads(r[3] or "{}")
            except Exception:
                metrics = {}
            out.append({
                "model_kind": r[0],
                "model_ts_ms": int(r[1]),
                "stage": r[2],
                "metrics": metrics,
                "created_ts_ms": int(r[4]),
                "note": r[5],
            })

        return {"ok": True, "rows": out}
    finally:
        con.close()

# ============================================================
# ADDITIONAL READ ENDPOINTS (moved from dashboard_server)
# ============================================================

def get_confidence_mass():
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


def get_temporal_eval(limit: int = 50):
    limit = max(1, min(5000, int(limit or 50)))
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

        return {
            "ok": True,
            "rows": [
                {
                    "horizon_s": int(r[0] or 0),
                    "n": int(r[1] or 0),
                    "rmse": float(r[2] or 0.0),
                    "directional_acc": float(r[3] or 0.0),
                    "ts_ms": int(r[4] or 0),
                }
                for r in rows
            ],
        }
    finally:
        con.close()


def get_embed_model_eval(limit: int = 500):
    limit = max(1, min(5000, int(limit or 500)))
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
        for r in rows:
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


def get_embed_conf_calib(horizon_s: int, model_kind: str, limit: int = 200):
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
                (hs, mk),
            ).fetchone()
        except Exception:
            row = None

        if not row:
            return {"ok": True, "curve": None}

        ts_ms, conf_k, n_points, xj, yj = row
        xs = json.loads(xj or "[]")
        ys = json.loads(yj or "[]")

        curve = []
        for i in range(min(len(xs), len(ys))):
            curve.append({"x": float(xs[i]), "y": float(ys[i])})

        return {
            "ok": True,
            "horizon_s": hs,
            "model_kind": mk,
            "ts_ms": int(ts_ms or 0),
            "conf_k": float(conf_k or 0.0),
            "n_points": int(n_points or len(curve)),
            "curve": curve,
        }
    finally:
        con.close()
