# jobs/eval_temporal_shadow.py
import time
from engine.dev_core.storage import connect, init_db

def main():
    init_db()
    now_ms = int(time.time() * 1000)

    con = connect()
    try:
        con.executescript("""
        INSERT OR REPLACE INTO temporal_shadow_eval (
          key_type,
          key,
          horizon_s,
          n,
          rmse,
          baseline_rmse,
          directional_acc,
          baseline_directional_acc,
          rmse_improvement,
          diracc_delta,
          pass_all,
          detail_json,
          ts_ms
        )
        SELECT
          key_type,
          key,
          horizon_s,
          n,
          rmse,
          baseline_rmse,
          directional_acc,
          baseline_directional_acc,
          rmse_improvement,
          diracc_delta,
          pass_all,
          detail_json,
          ts_ms
        FROM (
          /* === A.7 CORE QUERY === */
          WITH
          baseline AS (
            SELECT
              symbol,
              horizon_s,
              COUNT(*) AS n,
              SQRT(AVG((p.predicted_z - l.impact_z)*(p.predicted_z - l.impact_z))) AS rmse,
              AVG(CASE
                WHEN (p.predicted_z >= 0 AND l.impact_z >= 0)
                  OR (p.predicted_z < 0 AND l.impact_z < 0)
                THEN 1.0 ELSE 0.0 END) AS directional_acc
            FROM predictions p
            JOIN labels l
              ON l.event_id=p.event_id
             AND l.symbol=p.symbol
             AND l.horizon_s=p.horizon_s
            WHERE l.impact_z IS NOT NULL
            GROUP BY symbol, horizon_s
          ),
          temporal AS (
            SELECT
              symbol,
              horizon_s,
              COUNT(*) AS n,
              SQRT(AVG((t.pred_z - l.impact_z)*(t.pred_z - l.impact_z))) AS rmse,
              AVG(CASE
                WHEN (t.pred_z >= 0 AND l.impact_z >= 0)
                  OR (t.pred_z < 0 AND l.impact_z < 0)
                THEN 1.0 ELSE 0.0 END) AS directional_acc,
              MAX(t.model_ts_ms) AS latest_model_ts_ms
            FROM temporal_predictions t
            JOIN labels l
              ON l.event_id=t.event_id
             AND l.symbol=t.symbol
             AND l.horizon_s=t.horizon_s
            WHERE l.impact_z IS NOT NULL
            GROUP BY symbol, horizon_s
          )
          SELECT
            'symbol' AS key_type,
            t.symbol AS key,
            t.horizon_s,
            t.n,
            t.rmse,
            b.rmse AS baseline_rmse,
            t.directional_acc,
            b.directional_acc AS baseline_directional_acc,
            (b.rmse - t.rmse)/b.rmse AS rmse_improvement,
            (t.directional_acc - b.directional_acc) AS diracc_delta,
            CASE
              WHEN t.n >= 200
               AND t.rmse < b.rmse * 0.99
               AND t.directional_acc >= b.directional_acc - 0.01
              THEN 1 ELSE 0
            END AS pass_all,
            json_object(
              'latest_model_ts_ms', t.latest_model_ts_ms,
              'n', t.n
            ) AS detail_json,
            {now_ms} AS ts_ms
          FROM temporal t
          JOIN baseline b
            ON b.symbol=t.symbol
           AND b.horizon_s=t.horizon_s
        );
        """)
        con.commit()
        print("[A.7] temporal shadow evaluation complete")
    finally:
        con.close()

if __name__ == "__main__":
    main()
