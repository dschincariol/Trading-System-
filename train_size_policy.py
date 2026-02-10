# train_size_policy.py
"""
Learn a confidence -> size factor policy from realized net returns.

- Join predictions (confidence) with labels_exec (net_ret)
- Bucket by confidence
- For each bucket: mean(net_ret), std(net_ret)
- Convert to factor using (mean/std) vs a normalization constant
- Enforce monotone non-decreasing factors w.r.t confidence
- Store in size_policy + size_policy_points
"""

import os
import json
import time
import math
from typing import List, Tuple

from dev_core.storage import connect, init_db

from dev_core.training_guard import training_allowed

# ------            -- ------------------------------------------------------
# Schema (owned by this module)
# ------            -- ------------------------------------------------------
SIZE_POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS size_policy (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  lookback_days INTEGER NOT NULL,
  buckets INTEGER NOT NULL,
  method TEXT NOT NULL,
  params_json TEXT,
  metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS size_policy_points (
  policy_id INTEGER NOT NULL,
  bucket_idx INTEGER NOT NULL,
  conf_lo REAL NOT NULL,
  conf_hi REAL NOT NULL,
  n INTEGER NOT NULL,
  mean_net_ret REAL NOT NULL,
  std_net_ret REAL NOT NULL,
  factor REAL NOT NULL,
  PRIMARY KEY (policy_id, bucket_idx)
);
"""
def _ensure_size_policy_schema(con):
    """
    Backward-compatible schema fix for size_policy.
    Adds missing columns if the table already exists.
    """
    cols = {
        "lookback_days": "INTEGER",
        "buckets": "INTEGER",
    }

    existing = {
        r[1] for r in con.execute("PRAGMA table_info(size_policy)").fetchall()
    }

    for name, typ in cols.items():
        if name not in existing:
            con.execute(f"ALTER TABLE size_policy ADD COLUMN {name} {typ}")

if not training_allowed():
    print("[training_guard] training disabled")
    raise SystemExit(0)

LOOKBACK_DAYS = int(os.environ.get("SIZE_POLICY_LOOKBACK_DAYS", "90"))
BUCKETS = int(os.environ.get("SIZE_POLICY_BUCKETS", "10"))
MIN_SAMPLES = int(os.environ.get("SIZE_POLICY_MIN_SAMPLES", "200"))

# Convert bucket Sharpe proxy -> factor
SHARPE_NORM = float(os.environ.get("SIZE_POLICY_SHARPE_NORM", "1.0"))
MAX_FACTOR = float(os.environ.get("SIZE_POLICY_MAX_FACTOR", "1.0"))
MIN_FACTOR = float(os.environ.get("SIZE_POLICY_MIN_FACTOR", "0.0"))

# Prefer broker-fills if present
PREFER_REALIZED = os.environ.get("SIZE_POLICY_PREFER_REALIZED", "1") == "1"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _std(vals: List[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    var = sum((x - m) ** 2 for x in vals) / max(1, (len(vals) - 1))
    return math.sqrt(var)


def main():
    init_db()
    con = connect()

    # Ensure size_policy schema is compatible (idempotent)
    _ensure_size_policy_schema(con)

    # Ensure size policy schema exists (idempotent)
    con.executescript(SIZE_POLICY_SCHEMA)

    try:
        min_ts = _now_ms() - LOOKBACK_DAYS * 86400 * 1000

        # Join confidence -> net_ret
        # Prefer realized rows if enabled
        if PREFER_REALIZED:
            q = """
            SELECT p.confidence, le.net_ret
            FROM predictions p
            JOIN labels_exec le
              ON le.event_id=p.event_id AND le.symbol=p.symbol AND le.horizon_s=p.horizon_s
            WHERE p.ts_ms >= ?
              AND le.net_ret IS NOT NULL
              AND le.realized=1
            """
            rows = con.execute(q, (int(min_ts),)).fetchall()

            # If insufficient realized samples, fall back to any labels_exec
            if len(rows or []) < MIN_SAMPLES:
                q2 = """
                SELECT p.confidence, le.net_ret
                FROM predictions p
                JOIN labels_exec le
                  ON le.event_id=p.event_id AND le.symbol=p.symbol AND le.horizon_s=p.horizon_s
                WHERE p.ts_ms >= ?
                  AND le.net_ret IS NOT NULL
                """
                rows = con.execute(q2, (int(min_ts),)).fetchall()
        else:
            q2 = """
            SELECT p.confidence, le.net_ret
            FROM predictions p
            JOIN labels_exec le
              ON le.event_id=p.event_id AND le.symbol=p.symbol AND le.horizon_s=p.horizon_s
            WHERE p.ts_ms >= ?
              AND le.net_ret IS NOT NULL
            """
            rows = con.execute(q2, (int(min_ts),)).fetchall()

        data: List[Tuple[float, float]] = []
        for c, r in rows or []:
            try:
                cf = float(c)
                rr = float(r)
            except Exception:
                continue
            if not (0.0 <= cf <= 1.0):
                continue
            data.append((cf, rr))

        if len(data) < MIN_SAMPLES:
            raise SystemExit(f"[size_policy] not enough samples: {len(data)} < {MIN_SAMPLES}")

        # Build buckets
        buckets = [[] for _ in range(BUCKETS)]
        for cf, rr in data:
            idx = min(BUCKETS - 1, max(0, int(cf * BUCKETS)))
            buckets[idx].append(rr)

        points = []
        for i in range(BUCKETS):
            clo = i / BUCKETS
            chi = (i + 1) / BUCKETS if i < BUCKETS - 1 else 1.0
            vals = buckets[i]
            n = len(vals)
            mean = sum(vals) / n if n > 0 else 0.0
            sd = _std(vals)

            sharpe = 0.0
            if sd > 1e-12:
                sharpe = mean / sd  # simple proxy

            # Convert to [0..1] factor
            f = sharpe / max(1e-9, SHARPE_NORM)
            if f < 0.0:
                f = 0.0
            f = max(MIN_FACTOR, min(MAX_FACTOR, f))

            points.append({
                "bucket_idx": i,
                "conf_lo": float(clo),
                "conf_hi": float(chi),
                "n": int(n),
                "mean_net_ret": float(mean),
                "std_net_ret": float(sd),
                "factor": float(f),
            })

        # Enforce monotone non-decreasing factor with confidence
        # (higher confidence should never get smaller size)
        last = 0.0
        for p in points:
            if p["factor"] < last:
                p["factor"] = last
            else:
                last = p["factor"]

        # Store policy
        ts = _now_ms()
        params = {
            "lookback_days": LOOKBACK_DAYS,
            "buckets": BUCKETS,
            "min_samples": MIN_SAMPLES,
            "sharpe_norm": SHARPE_NORM,
            "prefer_realized": bool(PREFER_REALIZED),
        }
        metrics = {
            "n_samples": len(data),
            "method": "bucket_sharpe_monotone",
        }

        

        con.execute(
            """
            INSERT INTO size_policy(ts_ms, lookback_days, buckets, method, params_json, metrics_json)
            VALUES (?,?,?,?,?,?)
            """,
            (int(ts), int(LOOKBACK_DAYS), int(BUCKETS), "bucket_sharpe_monotone", json.dumps(params), json.dumps(metrics)),
        )
        pid = con.execute("SELECT last_insert_rowid()").fetchone()[0]

        for p in points:
            pass

        con.execute(
                """
                INSERT INTO size_policy_points(policy_id, bucket_idx, conf_lo, conf_hi, n, mean_net_ret, std_net_ret, factor)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    int(pid),
                    int(p["bucket_idx"]),
                    float(p["conf_lo"]),
                    float(p["conf_hi"]),
                    int(p["n"]),
                    float(p["mean_net_ret"]),
                    float(p["std_net_ret"]),
                    float(p["factor"]),
                ),
            )

        con.commit()
        print(f"[size_policy] stored policy_id={pid} n_samples={len(data)}")

    finally:
        con.close()


if __name__ == "__main__":
    main()
