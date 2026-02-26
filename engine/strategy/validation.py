# dev_core/validation.py
import json
import time
from engine.runtime.storage import connect
from engine.execution.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  event_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  predicted_z REAL NOT NULL,
  confidence REAL NOT NULL,
  UNIQUE(event_id, symbol, horizon_s)
);

CREATE TABLE IF NOT EXISTS validation_scores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  mae REAL NOT NULL,
  rmse REAL NOT NULL,
  n INTEGER NOT NULL,
  UNIQUE(symbol, horizon_s)
);

-- New: richer offline diagnostics for dashboard + tracking upgrades
CREATE TABLE IF NOT EXISTS model_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  model_name TEXT NOT NULL,
  symbol TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  n INTEGER NOT NULL,
  metrics_json TEXT NOT NULL,
  UNIQUE(model_name, symbol, horizon_s)
);

CREATE TABLE IF NOT EXISTS temporal_predictions (
  event_id INTEGER NOT NULL,
  ts_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  predicted_z REAL NOT NULL,
  confidence REAL NOT NULL,
  explain_json TEXT,
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY (event_id, symbol, horizon_s)
);

"""

def init_validation_db():
    con = connect()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()

def store_prediction(event_id, symbol, horizon_s, predicted_z, confidence):
    con = connect()
    try:
        

        con.execute(
            """
            INSERT INTO predictions(
              ts_ms, event_id, symbol, horizon_s, predicted_z, confidence
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, symbol, horizon_s) DO UPDATE SET
              ts_ms=excluded.ts_ms,
              predicted_z=excluded.predicted_z,
              confidence=excluded.confidence
            """,
            (
                int(time.time() * 1000),
                int(event_id),
                str(symbol),
                int(horizon_s),
                float(predicted_z),
                float(confidence),
            ),
        )
        con.commit()
    finally:
        con.close()

def compute_validation_scores():
    """
    Joins predictions with realized labels and computes MAE / RMSE.
    """
    con = connect()
    try:
        # Fail-soft if labels table isn't created yet
        try:
            con.execute("SELECT 1 FROM labels LIMIT 1").fetchone()
        except Exception:
            return 0

        rows = con.execute(
            """
            SELECT
              p.symbol,
              p.horizon_s,
              p.predicted_z,
              l.impact_z
            FROM predictions p
            JOIN labels l
              ON l.event_id = p.event_id
             AND l.symbol = p.symbol
             AND l.horizon_s = p.horizon_s
            """
        ).fetchall()

        if not rows:
            return 0

        from collections import defaultdict
        import math

        acc = defaultdict(list)
        for sym, h, pred, real in rows:
            acc[(sym, h)].append((float(pred), float(real)))

        now_ms = int(time.time() * 1000)
        cur = con.cursor()

        for (sym, h), vals in acc.items():
            n = len(vals)
            errs = [(p - r) for p, r in vals]
            mae = sum(abs(e) for e in errs) / n
            rmse = math.sqrt(sum(e * e for e in errs) / n)

            cur.execute(
                """
                INSERT INTO validation_scores(
                  ts_ms, symbol, horizon_s, mae, rmse, n
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, horizon_s) DO UPDATE SET
                  ts_ms=excluded.ts_ms,
                  mae=excluded.mae,
                  rmse=excluded.rmse,
                  n=excluded.n
                """,
                (now_ms, str(sym), int(h), float(mae), float(rmse), int(n)),
            )

        con.commit()
        return len(acc)
    finally:
        con.close()

def _compute_metrics_for_group(vals, err_threshold=1.0, n_bins=10):
    """
    vals: list[(pred_z, real_z, conf)]
    Confidence is expected in [0,1].
    Calibration: bin by confidence, compare avg_conf to "accuracy"
      where accuracy = P(|pred-real| <= err_threshold)
    """
    import math

    n = len(vals)
    preds = [float(p) for p, _, _ in vals]
    reals = [float(r) for _, r, _ in vals]
    confs = [float(c) for _, _, c in vals]

    errs = [p - r for p, r in zip(preds, reals)]
    abs_errs = [abs(e) for e in errs]

    mae = sum(abs_errs) / n
    rmse = math.sqrt(sum(e * e for e in errs) / n)

    # R2 (guard against zero variance)
    y_mean = sum(reals) / n
    ss_tot = sum((r - y_mean) ** 2 for r in reals)
    ss_res = sum((r - p) ** 2 for p, r in zip(preds, reals))
    r2 = 0.0 if ss_tot <= 1e-12 else float(1.0 - (ss_res / ss_tot))

    # Direction accuracy (ignore exact zeros)
    def sgn(x):
        if x > 0: return 1
        if x < 0: return -1
        return 0

    dir_hits = 0
    dir_n = 0
    for p, r in zip(preds, reals):
        sp = sgn(p)
        sr = sgn(r)
        if sp == 0 or sr == 0:
            continue
        dir_n += 1
        if sp == sr:
            dir_hits += 1
    dir_acc = float(dir_hits / dir_n) if dir_n else 0.0

    # Calibration (ECE-style)
    nb = max(2, min(50, int(n_bins)))
    bins = [{"n": 0, "avg_conf": 0.0, "acc": 0.0} for _ in range(nb)]
    for ae, c in zip(abs_errs, confs):
        cc = c
        if cc != cc:  # NaN
            cc = 0.0
        cc = max(0.0, min(1.0, float(cc)))
        bi = min(nb - 1, int(cc * nb))  # cc=1.0 -> nb, clamp
        bins[bi]["n"] += 1
        bins[bi]["avg_conf"] += cc
        bins[bi]["acc"] += 1.0 if ae <= float(err_threshold) else 0.0

    ece = 0.0
    for b in bins:
        if b["n"] <= 0:
            continue
        b["avg_conf"] = b["avg_conf"] / b["n"]
        b["acc"] = b["acc"] / b["n"]
        frac = b["n"] / n
        ece += frac * abs(b["acc"] - b["avg_conf"])

    out = {
        "mae": float(mae),
        "rmse": float(rmse),
        "r2": float(r2),
        "direction_acc": float(dir_acc),
        "avg_conf": float(sum(confs) / n),
        "err_threshold": float(err_threshold),
        "ece": float(ece),
        "abs_err_p50": float(sorted(abs_errs)[int(0.50 * (n - 1))]),
        "abs_err_p90": float(sorted(abs_errs)[int(0.90 * (n - 1))]),
        "bins": bins,
    }
    return out

def compute_model_metrics(model_name="default", err_threshold=1.0, n_bins=10):
    """
    Joins predictions with realized labels and computes richer metrics:
      MAE/RMSE/R2/direction_acc + confidence calibration (ECE-style).

    Stored in model_metrics(metrics_json).
    """
    con = connect()
    try:
        # Fail-soft if labels table isn't created yet
        try:
            con.execute("SELECT 1 FROM labels LIMIT 1").fetchone()
        except Exception:
            return 0

        rows = con.execute(
            """
            SELECT
              p.symbol,
              p.horizon_s,
              p.predicted_z,
              l.impact_z,
              p.confidence
            FROM predictions p
            JOIN labels l
              ON l.event_id = p.event_id
             AND l.symbol = p.symbol
             AND l.horizon_s = p.horizon_s
            """
        ).fetchall()

        if not rows:
            return 0

        from collections import defaultdict
        acc = defaultdict(list)
        for sym, h, pred, real, conf in rows:
            try:
                acc[(str(sym), int(h))].append((float(pred), float(real), float(conf)))
            except Exception:
                continue

        now_ms = int(time.time() * 1000)
        cur = con.cursor()

        for (sym, h), vals in acc.items():
            n = len(vals)
            metrics = _compute_metrics_for_group(vals, err_threshold=float(err_threshold), n_bins=int(n_bins))
            metrics_json = json.dumps(metrics, separators=(",", ":"), sort_keys=True)

            cur.execute(
                """
                INSERT INTO model_metrics(
                  ts_ms, model_name, symbol, horizon_s, n, metrics_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_name, symbol, horizon_s) DO UPDATE SET
                  ts_ms=excluded.ts_ms,
                  n=excluded.n,
                  metrics_json=excluded.metrics_json
                """,
                (now_ms, str(model_name), str(sym), int(h), int(n), metrics_json),
            )

        con.commit()
        return len(acc)
    finally:
        con.close()

def get_validation_scores():
    con = connect()
    try:
        return con.execute(
            """
            SELECT symbol, horizon_s, mae, rmse, n, ts_ms
            FROM validation_scores
            ORDER BY symbol, horizon_s
            """
        ).fetchall()
    finally:
        con.close()

def get_model_metrics(model_name="default"):
    con = connect()
    try:
        # Fail-soft if labels table isn't created yet
        try:
            con.execute("SELECT 1 FROM labels LIMIT 1").fetchone()
        except Exception:
            return 0

        rows = con.execute(
            """
            SELECT symbol, horizon_s, n, ts_ms, metrics_json
            FROM model_metrics
            WHERE model_name=?
            ORDER BY symbol, horizon_s
            """,
            (str(model_name),),
        ).fetchall()

        out = []
        for sym, h, n, ts_ms, mj in rows:
            try:
                metrics = json.loads(mj) if mj else {}
            except Exception:
                metrics = {}
            out.append({
                "model_name": str(model_name),
                "symbol": str(sym),
                "horizon_s": int(h),
                "n": int(n),
                "ts_ms": int(ts_ms),
                "metrics": metrics,
            })
        return out
    finally:
        con.close()
