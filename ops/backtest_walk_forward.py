# backtest_walk_forward.py
"""
Walk-forward backtest (SQLite-native).

For each labeled event (in chronological order):
- build a "past-only" KNN predictor using event_embeddings + labels before this event ts
- predict expected_z
- evaluate vs realized impact_z

This is intentionally simple and honest:
- no leakage (uses only past)
- measures directional accuracy + MAE

Outputs summary per (symbol,horizon_s).
"""

import json
import math
import time
import os
import logging
import hashlib
from typing import Dict, Any, Tuple, Optional, List

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from engine.runtime.storage import connect, init_db

SYMBOLS = ["SPY", "BTC", "OIL"]
HORIZONS = [300, 3600]

TOP_K = int(os.environ.get("WF_TOP_K", "8"))
HALF_LIFE_DAYS = float(os.environ.get("WF_HALF_LIFE_DAYS", "7.0"))
WARMUP_MIN_PAST = int(os.environ.get("WF_WARMUP_MIN_PAST", "10"))
MAX_EVENTS = int(os.environ.get("WF_MAX_EVENTS", "0"))  # 0 = no limit

MS_PER_DAY = 24 * 3600 * 1000

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [backtest_walk_forward] %(message)s",
)


def _time_decay_weight(past_ts_ms: int, now_ms: int) -> float:
    age_days = max(0.0, (now_ms - past_ts_ms) / MS_PER_DAY)
    return math.exp(-age_days / max(1e-9, HALF_LIFE_DAYS))


def _make_run_id(params: Dict[str, Any]) -> str:
    raw = json.dumps(params, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _ensure_schema(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS walk_forward_runs (
          run_id TEXT PRIMARY KEY,
          params_json TEXT NOT NULL,
          metrics_json TEXT NOT NULL,
          ts_ms INTEGER NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS walk_forward_scores (
          run_id TEXT NOT NULL,
          symbol TEXT NOT NULL,
          horizon_s INTEGER NOT NULL,
          ts_ms INTEGER NOT NULL,
          n INTEGER NOT NULL,
          mae REAL NOT NULL,
          dir_acc REAL NOT NULL,
          PRIMARY KEY (run_id, symbol, horizon_s)
        )
        """
    )
    con.commit()


def main() -> int:
    init_db()
    init_validation_db()
    con = connect()
    try:
        _ensure_schema(con)

        params = {
            "symbols": SYMBOLS,
            "horizons": HORIZONS,
            "top_k": TOP_K,
            "half_life_days": HALF_LIFE_DAYS,
            "warmup_min_past": WARMUP_MIN_PAST,
            "max_events": MAX_EVENTS,
        }
        run_id = _make_run_id(params)

        # Pull labeled events with embeddings, chronological
        rows = con.execute(
            """
            SELECT e.id, e.ts_ms, emb.vec
            FROM events e
            JOIN event_embeddings emb ON emb.event_id = e.id
            WHERE EXISTS (
              SELECT 1 FROM labels l
              WHERE l.event_id=e.id AND l.impact_z IS NOT NULL
            )
            ORDER BY e.ts_ms ASC
            """
        ).fetchall()

        if not rows:
            print("No labeled embedded events found.")
            return 0

        if MAX_EVENTS and len(rows) > MAX_EVENTS:
            rows = rows[:MAX_EVENTS]

        # Labels map: (event_id, symbol, horizon_s) -> impact_z
        lbls = con.execute(
            """
            SELECT event_id, symbol, horizon_s, impact_z
            FROM labels
            WHERE impact_z IS NOT NULL
            """
        ).fetchall()
        label_map: Dict[Tuple[int, str, int], float] = {
            (int(eid), str(sym), int(h)): float(z) for (eid, sym, h, z) in lbls
        }

        # Pre-decode vectors and timestamps
        ids: List[int] = [int(r[0]) for r in rows]
        tss: List[int] = [int(r[1]) for r in rows]
        vecs: List[np.ndarray] = [np.frombuffer(r[2], dtype=np.float32) for r in rows]

        # Accumulators per (sym,h)
        agg: Dict[Tuple[str, int], Dict[str, Any]] = {}
        for sym in SYMBOLS:
            for h in HORIZONS:
                agg[(str(sym), int(h))] = {"n": 0, "mae_sum": 0.0, "dir_ok": 0}

        # Walk-forward loop
        for i in range(len(ids)):
            eid = ids[i]
            ts_now = tss[i]
            q = vecs[i]

            if i < WARMUP_MIN_PAST:
                continue

            past_ids = ids[:i]
            past_ts = tss[:i]
            past_vecs = np.stack(vecs[:i]).astype(np.float32, copy=False)

            sims = cosine_similarity([q], past_vecs)[0]

            for sym in SYMBOLS:
                for h in HORIZONS:
                    key_now = (int(eid), str(sym), int(h))
                    if key_now not in label_map:
                        continue

                    scored = []
                    for peid, pts, sim in zip(past_ids, past_ts, sims):
                        if sim <= 0:
                            continue
                        k = (int(peid), str(sym), int(h))
                        if k not in label_map:
                            continue
                        decay = _time_decay_weight(int(pts), int(ts_now))
                        w = float(sim) * float(decay)
                        if w <= 0:
                            continue
                        scored.append((w, float(label_map[k])))

                    if not scored:
                        continue

                    scored.sort(reverse=True, key=lambda x: x[0])
                    top = scored[:TOP_K]
                    wsum = sum(w for w, _ in top)
                    if wsum <= 0:
                        continue

                    pred = sum(w * z for w, z in top) / wsum
                    real = float(label_map[key_now])

                    m = agg[(str(sym), int(h))]
                    m["n"] += 1
                    m["mae_sum"] += abs(pred - real)
                    if (pred >= 0 and real >= 0) or (pred < 0 and real < 0):
                        m["dir_ok"] += 1

        # Finalize metrics and store
        now_ms = int(time.time() * 1000)
        summary: Dict[str, Any] = {
            "run_id": run_id,
            "params": params,
            "per_key": {},
        }

        for sym in SYMBOLS:
            for h in HORIZONS:
                m = agg[(str(sym), int(h))]
                n = int(m["n"])
                if n <= 0:
                    continue
                mae = float(m["mae_sum"]) / float(n)
                acc = float(m["dir_ok"]) / float(n)

                summary["per_key"][f"{sym}:{h}"] = {"n": n, "mae": mae, "dir_acc": acc}

                pass

        con.execute(
                    """
                    INSERT OR REPLACE INTO walk_forward_scores(run_id, symbol, horizon_s, ts_ms, n, mae, dir_acc)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(run_id),
                        str(sym),
                        int(h),
                        int(now_ms),
                        int(n),
                        float(mae),
                        float(acc),
                    ),
                )

        # Run-level metrics (simple aggregates over keys)
        all_keys = list(summary["per_key"].values())
        if all_keys:
            total_n = sum(int(x.get("n", 0)) for x in all_keys)
            w_mae = 0.0
            w_acc = 0.0
            for x in all_keys:
                wn = float(x.get("n", 0))
                if wn <= 0:
                    continue
                w_mae += wn * float(x.get("mae", 0.0))
                w_acc += wn * float(x.get("dir_acc", 0.0))
            run_metrics = {
                "total_n": int(total_n),
                "mae": float(w_mae / max(1.0, float(total_n))),
                "dir_acc": float(w_acc / max(1.0, float(total_n))),
                "n_keys": int(len(all_keys)),
            }
        else:
            run_metrics = {"total_n": 0, "mae": None, "dir_acc": None, "n_keys": 0}

        

        con.execute(
            """
            INSERT OR REPLACE INTO walk_forward_runs(run_id, params_json, metrics_json, ts_ms)
            VALUES (?, ?, ?, ?)
            """,
            (str(run_id), json.dumps(params), json.dumps(run_metrics), int(now_ms)),
        )

        con.commit()

        logging.info(
            "WALK_FORWARD run=%s total_n=%s mae=%s dir_acc=%s n_keys=%s",
            str(run_id),
            int(run_metrics.get("total_n") or 0),
            run_metrics.get("mae"),
            run_metrics.get("dir_acc"),
            int(run_metrics.get("n_keys") or 0),
        )

        print("\nWALK-FORWARD SUMMARY")
        for sym in SYMBOLS:
            for h in HORIZONS:
                k = f"{sym}:{h}"
                if k not in summary["per_key"]:
                    continue
                x = summary["per_key"][k]
                print(f"{sym} h={h} n={int(x['n'])} MAE={float(x['mae']):.3f} DirAcc={float(x['dir_acc']):.3f}")

        print("\nRUN METRICS")
        print(json.dumps({"run_id": run_id, "metrics": run_metrics}, indent=2))

        print("\nDONE (tables: walk_forward_runs, walk_forward_scores)")
        return 0

    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
