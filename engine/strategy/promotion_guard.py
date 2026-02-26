# dev_core/promotion_guard.py
import math
import os
import logging
import time
from typing import Dict, Any, Tuple

from engine.runtime.storage import connect, init_db

# ------            -- ------------------------------------------------------
# Global enable switch (env default ON)
# ------            -- ------------------------------------------------------

PROMOTION_ENABLED_ENV = os.environ.get("PROMOTION_ENABLED", "1") == "1"

# ------            -- ------------------------------------------------------
# Guard thresholds
# ------            -- ------------------------------------------------------

PROMOTION_CRIT_ALERT_LOOKBACK_S = int(os.environ.get("PROMOTION_CRIT_ALERT_LOOKBACK_S", "7200"))  # 2h
PROMOTION_MAX_CRIT_ALERTS = int(os.environ.get("PROMOTION_MAX_CRIT_ALERTS", "0"))  # 0 => any CRIT blocks

PROMOTION_MAX_DRIFT_RATIO = float(os.environ.get("PROMOTION_MAX_DRIFT_RATIO", "0.0"))  # 0 disables
PROMOTION_DRIFT_LOOKBACK_S = int(os.environ.get("PROMOTION_DRIFT_LOOKBACK_S", "86400"))  # 24h

PROMOTION_EQUITY_DRIFT_LOOKBACK_S = int(
    os.environ.get("PROMOTION_EQUITY_DRIFT_LOOKBACK_S", "7200")
)  # 2h
PROMOTION_BLOCK_IF_EQUITY_CRIT = os.environ.get(
    "PROMOTION_BLOCK_IF_EQUITY_CRIT", "1"
) == "1"

# ------            -- ------------------------------------------------------
# Logging
# ------            -- ------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [promotion_guard] %(message)s",
)

# ------            -- ------------------------------------------------------
# Coverage / sanity thresholds (metric-based promotion)
# ------            -- ------------------------------------------------------

MIN_EVAL_ROWS = int(os.environ.get("PROMOTE_MIN_EVAL_ROWS", "200"))
MAX_ABS_RMSE = float(os.environ.get("PROMOTE_MAX_ABS_RMSE", "10.0"))
MAX_ABS_BIAS = float(os.environ.get("PROMOTE_MAX_ABS_BIAS", "5.0"))

# ------            -- ------------------------------------------------------
# Time helper
# ------            -- ------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)

# ------            -- ------------------------------------------------------
# Guard state (DB overrides env)
# ------            -- ------------------------------------------------------

def set_guard(key: str, value: str) -> None:
    init_db()
    con = connect()
    try:
        

        con.execute(
            """
            INSERT OR REPLACE INTO model_promotion_guard(key, value, updated_ts_ms)
            VALUES (?,?,?)
            """,
            (str(key), str(value), _now_ms()),
        )
        con.commit()
    finally:
        con.close()


def get_guard(key: str, default: str) -> str:
    init_db()
    con = connect()
    try:
        r = con.execute(
            "SELECT value FROM model_promotion_guard WHERE key=?",
            (str(key),),
        ).fetchone()
        return str(r[0]) if r and r[0] is not None else str(default)
    finally:
        con.close()

# ------            -- ------------------------------------------------------
# A) Metric-based promotion decision (RESTORED, not lost)
# ------            -- ------------------------------------------------------

def promotion_allowed_by_metrics(
    challenger_metrics: Dict[str, Any],
    champion_metrics: Dict[str, Any],
    min_improvement: float,
    diracc_tol: float,
) -> bool:
    """
    Pure metric-based promotion gate.
    Returns True if challenger is statistically better.
    """
    try:
        # ---- coverage ----
        n_eval = int(challenger_metrics.get("n_eval", 0))
        if n_eval < MIN_EVAL_ROWS:
            logging.warning("PROMOTE_BLOCKED insufficient_eval_rows=%s", n_eval)
            return False

        # ---- sanity ----
        rmse = float(challenger_metrics.get("rmse", float("inf")))
        bias = abs(float(challenger_metrics.get("bias", 0.0)))

        if not math.isfinite(rmse) or rmse > MAX_ABS_RMSE:
            logging.warning("PROMOTE_BLOCKED bad_rmse=%s", rmse)
            return False

        if not math.isfinite(bias) or bias > MAX_ABS_BIAS:
            logging.warning("PROMOTE_BLOCKED bad_bias=%s", bias)
            return False

        # ---- improvement ----
        champ_rmse = float(champion_metrics.get("rmse", float("inf")))
        if rmse >= champ_rmse * (1.0 - min_improvement):
            logging.info(
                "PROMOTE_BLOCKED no_rmse_improvement rmse=%s champ=%s",
                rmse,
                champ_rmse,
            )
            return False

        # ---- directional accuracy ----
        ch_dir = float(challenger_metrics.get("dir_acc", 0.0))
        cp_dir = float(champion_metrics.get("dir_acc", 0.0))
        if ch_dir < cp_dir - diracc_tol:
            logging.info(
                "PROMOTE_BLOCKED dir_acc_worse ch=%s cp=%s",
                ch_dir,
                cp_dir,
            )
            return False

        logging.info(
            "PROMOTE_ALLOWED metrics rmse=%s dir_acc=%s n_eval=%s",
            rmse,
            ch_dir,
            n_eval,
        )
        return True

    except Exception as e:
        logging.error("PROMOTE_BLOCKED metrics exception=%r", e)
        return False

# ------            -- ------------------------------------------------------
# B) System-state promotion guard (public API)
# ------            -- ------------------------------------------------------

def promotion_allowed() -> Tuple[bool, Dict[str, Any]]:
    """
    System-wide promotion guard.
    Returns (allowed, reason_dict).
    """
    init_db()

    enabled_db = get_guard("promotion_enabled", "1")
    enabled = (enabled_db == "1") and PROMOTION_ENABLED_ENV

    reason: Dict[str, Any] = {
        "promotion_enabled_env": bool(PROMOTION_ENABLED_ENV),
        "promotion_enabled_db": enabled_db,
        "blockers": [],
    }

    if not enabled:
        reason["blockers"].append("disabled")
        return (False, reason)

    con = connect()
    try:
        now = _now_ms()

        # ---- cooldown guard (global, fail-closed) ----
        cooldown_s = int(os.environ.get("PROMOTION_COOLDOWN_S", "21600"))  # 6h
        cooldown_ms = int(cooldown_s) * 1000
        try:
            last_promo = con.execute(
                """
                SELECT MAX(ts_ms) FROM model_promotion_audit
                WHERE action='promote'
                """
            ).fetchone()[0]
            last_promo = int(last_promo or 0)
        except Exception:
            last_promo = 0

        reason["last_promo_ts_ms"] = last_promo
        reason["cooldown_s"] = int(cooldown_s)

        if last_promo > 0 and (now - last_promo) < cooldown_ms:
            reason["blockers"].append("cooldown")

        # ---- CRIT alerts guard ----
        try:
            lookback_ms = PROMOTION_CRIT_ALERT_LOOKBACK_S * 1000
            n_crit = con.execute(
                """
                SELECT COUNT(1) FROM alerts
                WHERE severity='CRIT' AND ts_ms >= ?
                """,
                (now - lookback_ms,),
            ).fetchone()[0]
            n_crit = int(n_crit or 0)
        except Exception:
            n_crit = 0

        reason["crit_alerts"] = n_crit
        if n_crit > PROMOTION_MAX_CRIT_ALERTS:
            reason["blockers"].append("crit_alerts")

        # ---- equity drift CRIT ----
        if PROMOTION_BLOCK_IF_EQUITY_CRIT:
            try:
                ed_ms = PROMOTION_EQUITY_DRIFT_LOOKBACK_S * 1000
                ed = con.execute(
                    """
                    SELECT COUNT(1) FROM equity_drift
                    WHERE level='CRIT' AND ts_ms >= ?
                    """,
                    (now - ed_ms,),
                ).fetchone()[0]
                ed = int(ed or 0)
            except Exception:
                ed = 0

            reason["equity_drift_crit_points"] = ed
            if ed > 0:
                reason["blockers"].append("equity_drift_crit")

        # ---- model drift ratio ----
        if PROMOTION_MAX_DRIFT_RATIO > 0.0:
            try:
                md = con.execute(
                    "SELECT MAX(drift_ratio) FROM model_drift"
                ).fetchone()[0]
                md = float(md or 0.0)
            except Exception:
                md = 0.0

            reason["max_drift_ratio"] = md
            if md > PROMOTION_MAX_DRIFT_RATIO:
                reason["blockers"].append("drift_ratio")

    finally:
        con.close()

    # ------------------------------------------------------------
    # Trade Attribution Guard (capital-based pruning)
    # ------------------------------------------------------------
    try:
        rows = con.execute(
            """
            SELECT
              json_extract(model_json, '$.model_name') AS model_name,
              SUM(COALESCE(pnl,0)) AS total_pnl
            FROM trade_attribution_ledger
            WHERE suppression_reason IS NULL
              AND ts_ms >= ?
            GROUP BY model_name
            """,
            (now - (PROMOTION_DRIFT_LOOKBACK_S * 1000),),
        ).fetchall()

        model_pnl = {str(r[0]): float(r[1] or 0.0) for r in rows if r[0]}

        reason["model_pnl_snapshot"] = model_pnl

        # block promotion if any live model is negative capital impact
        negative_models = [m for m, p in model_pnl.items() if float(p) < 0.0]
        if negative_models:
            reason["blockers"].append("negative_real_pnl_models")
            reason["negative_models"] = negative_models

    except Exception:
        pass

    allowed = len(reason["blockers"]) == 0
    return (allowed, reason)
