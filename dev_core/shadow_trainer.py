# dev_core/shadow_trainer.py
import json
import time
from typing import Optional

from dev_core.storage import connect
from dev_core.model_registry import register_model
from dev_core.training_guard import training_allowed
from dev_core.model_v2 import train_regime_model

def _now_ms():
    return int(time.time() * 1000)

def train_shadow(
    *,
    model_name: str,
    horizon_s: int,
    regime: Optional[str] = None,
    min_rows: int = 100,
) -> None:
    if not training_allowed():
        return

    con = connect()
    run_id = None
    try:
        rows = con.execute(
            """
            SELECT event_id, symbol, horizon_s, realized_z, vol_proxy, regime
            FROM labels
            WHERE horizon_s=?
              AND realized_z IS NOT NULL
              AND (? IS NULL OR regime=?)
            ORDER BY ts_ms DESC
            """,
            (horizon_s, regime, regime),
        ).fetchall()

        if len(rows) < min_rows:
            return

        run_id = con.execute(
            """
            INSERT INTO shadow_training_runs
              (ts_ms, model_name, regime, horizon_s, train_rows, metrics_json, status)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                _now_ms(),
                model_name,
                regime,
                horizon_s,
                len(rows),
                "{}",
                "running",
            ),
        ).lastrowid
        con.commit()

        model, metrics = train_regime_model(
            rows=rows,
            horizon_s=horizon_s,
            regime=regime,
            shadow=True,
        )

        register_model(
            model_name=model_name,
            model=model,
            stage="shadow",
            horizon_s=horizon_s,
            regime=regime,
            metrics=metrics,
        )

        con.execute(
            """
            UPDATE shadow_training_runs
            SET status='ok', metrics_json=?
            WHERE id=?
            """,
            (json.dumps(metrics or {}, separators=(",", ":"), sort_keys=True), run_id),
        )
        con.commit()

    except Exception as e:
        if run_id is not None:
            con.execute(
                """
                UPDATE shadow_training_runs
                SET status='error', error=?
                WHERE id=?
                """,
                (str(e), run_id),
            )
            con.commit()
    finally:
        con.close()
