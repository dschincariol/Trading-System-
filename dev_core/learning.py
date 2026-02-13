import math
from typing import Tuple, Dict

from dev_core.storage import connect
from dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot


# ------------            -- ------------------------------------------------------
# Confidence helpers (UNCHANGED)
# ------------            -- ------------------------------------------------------

def confidence_from_n(n: int) -> float:
    n = max(0, int(n))
    return float(1.0 - math.exp(-n / 25.0))


def confidence_from_weight(w: float) -> float:
    w = max(0.0, float(w))
    return float(1.0 - math.exp(-w / 3.0))


# ------------            -- ------------------------------------------------------
# Global priors (UNCHANGED)
# ------------            -- ------------------------------------------------------

def _ensure_model_stats():
    con = connect()
    try:
        

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS model_stats (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              symbol TEXT NOT NULL,
              horizon_s INTEGER NOT NULL,
              n INTEGER NOT NULL,
              mean_impact_z REAL NOT NULL,
              UNIQUE(symbol, horizon_s)
            )
            """
        )
        con.commit()
    finally:
        con.close()


def get_global_prior(symbol: str, horizon_s: int) -> Tuple[float, int]:
    """
    Returns (mean_z, n). If missing, returns (0.0, 0).
    """
    _ensure_model_stats()
    con = connect()
    try:
        try:
            row = con.execute(
                """
                SELECT mean_impact_z, n
                FROM model_stats
                WHERE symbol=? AND horizon_s=?
                """,
                (str(symbol), int(horizon_s)),
            ).fetchone()
        except Exception:
            return 0.0, 0

        if not row:
            return 0.0, 0

        return float(row[0]), int(row[1])
    finally:
        con.close()


# ------------            -- ------------------------------------------------------
# OPTION 5 — Learned relevance from real outcomes (NEW, SAFE)
# ------------            -- ------------------------------------------------------

def learn_relevance_stats(
    abs_z_threshold: float = 0.5,
) -> Dict[str, Dict[int, Dict[str, float]]]:
    """
    Learns empirical event→asset relevance from labels table.

    Definition (simple + robust):
      relevance = P(|impact_z| >= abs_z_threshold)

    Returns:
      {
        symbol: {
          horizon_s: {
            "relevance": float in [0,1],
            "n": int
          }
        }
      }

    Notes:
    - Uses ONLY existing tables (labels)
    - No schema changes
    - No side effects (read-only)
    - Stable, explainable, auditable
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
            SELECT symbol, horizon_s, impact_z
            FROM labels
            WHERE impact_z IS NOT NULL
            """
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return {}

    from collections import defaultdict

    total = defaultdict(int)
    hits = defaultdict(int)

    for sym, h, z in rows:
        try:
            z = float(z)
        except Exception:
            continue

        key = (str(sym), int(h))
        total[key] += 1
        if abs(z) >= float(abs_z_threshold):
            hits[key] += 1

    out: Dict[str, Dict[int, Dict[str, float]]] = {}

    for (sym, h), n in total.items():
        if n <= 0:
            continue
        rel = float(hits[(sym, h)] / n)
        out.setdefault(sym, {})[h] = {
            "relevance": float(rel),
            "n": int(n),
        }

    return out
