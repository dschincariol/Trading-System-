# dev_core/model_v2.py
import math
import time
from typing import Dict, List, Optional, Tuple

from engine.runtime.storage import connect
from engine.execution.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_stats_regime (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  regime TEXT NOT NULL,
  n INTEGER NOT NULL,
  mean_impact_z REAL NOT NULL,
  UNIQUE(symbol, horizon_s, regime)
);

CREATE TABLE IF NOT EXISTS model_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  n INTEGER NOT NULL,
  mean_impact_z REAL NOT NULL,
  UNIQUE(symbol, horizon_s)
);

CREATE TABLE IF NOT EXISTS spillover_beta (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  target_symbol TEXT NOT NULL,
  driver_symbol TEXT NOT NULL,
  horizon_s INTEGER NOT NULL,
  n INTEGER NOT NULL,
  beta REAL NOT NULL,
  UNIQUE(target_symbol, driver_symbol, horizon_s)
);

CREATE INDEX IF NOT EXISTS idx_msreg_sym ON model_stats_regime(symbol, horizon_s);
CREATE INDEX IF NOT EXISTS idx_ms_sym ON model_stats(symbol, horizon_s);
CREATE INDEX IF NOT EXISTS idx_spill_tgt ON spillover_beta(target_symbol, horizon_s);
"""

DEFAULT_REGIMES = ["LOW", "MID", "HIGH"]


def init_model_db():
    con = connect()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def classify_regime(vol: float) -> str:
    """
    Deterministic regime bucketing from a volatility proxy.
    Used at label-time (labeling.py, label_due_events.py).

    Thresholds aligned to get_current_regime() levels:
      LOW  if vol < 0.004
      HIGH if vol > 0.012
      MID  otherwise

    If vol is missing/invalid, returns MID.
    """
    try:
        v = float(vol)
    except Exception:
        return "MID"

    if not math.isfinite(v) or v <= 0:
        return "MID"

    if v < 0.004:
        return "LOW"
    if v > 0.012:
        return "HIGH"
    return "MID"


def get_current_regime(symbol: str) -> str:
    # lightweight: derived from recent realized volatility in prices
    con = connect()
    try:
        try:
            rows = con.execute(
                """
                SELECT price
                FROM prices
                WHERE symbol=?
                ORDER BY ts_ms DESC
                LIMIT 120
                """,
                (str(symbol),),
            ).fetchall()
        except Exception:
            return "MID"

        if not rows or len(rows) < 30:
            return "MID"

        px = [float(r[0]) for r in rows if r and r[0] is not None]
        if len(px) < 30:
            return "MID"

        rets = []
        for i in range(1, len(px)):
            if px[i - 1] <= 0:
                continue
            rets.append((px[i] / px[i - 1]) - 1.0)
        if len(rets) < 20:
            return "MID"

        vol = float(math.sqrt(sum(r * r for r in rets) / len(rets)))
        return classify_regime(vol)
    finally:
        con.close()


def get_regime_prior(symbol: str, horizon_s: int) -> Tuple[float, int, str]:
    """
    Returns (mean_z, n, regime).
    If no regime row exists, returns (0,0,regime).
    """
    init_model_db()
    reg = get_current_regime(str(symbol))
    con = connect()
    try:
        try:
            row = con.execute(
                """
                SELECT mean_impact_z, n
                FROM model_stats_regime
                WHERE symbol=? AND horizon_s=? AND regime=?
                """,
                (str(symbol), int(horizon_s), str(reg)),
            ).fetchone()
        except Exception:
            return 0.0, 0, str(reg)

        if not row:
            return 0.0, 0, str(reg)

        return float(row[0]), int(row[1]), str(reg)
    finally:
        con.close()


def get_spillover_betas(target_symbol: str, horizon_s: int) -> List[Tuple[str, float, int]]:
    """
    Returns list of (driver_symbol, beta, n) for target_symbol/horizon_s.
    """
    init_model_db()
    con = connect()
    try:
        try:
            rows = con.execute(
                """
                SELECT driver_symbol, beta, n
                FROM spillover_beta
                WHERE target_symbol=? AND horizon_s=?
                ORDER BY n DESC
                """,
                (str(target_symbol), int(horizon_s)),
            ).fetchall()
        except Exception:
            return []

        out = []
        for drv, beta, n in rows or []:
            out.append((str(drv), float(beta), int(n)))
        return out
    finally:
        con.close()


def train_regime_stats(symbols: List[str], horizons: List[int], lookback_days: int = 90) -> int:
    """
    Builds:
      - model_stats_regime (per regime mean + n)
      - model_stats (global mean + n)
    using labels joined with prices-derived regime at prediction time.
    """
    init_model_db()
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - int(lookback_days) * 24 * 3600 * 1000

    con = connect()
    try:
        # labels table must exist
        try:
            rows = con.execute(
                """
                SELECT l.symbol, l.horizon_s, l.impact_z
                FROM labels l
                JOIN events e ON e.id = l.event_id
                WHERE e.ts_ms >= ?
                  AND l.impact_z IS NOT NULL
                """,
                (int(cutoff_ms),),
            ).fetchall()
        except Exception:
            return 0

        # bucket by current regime of symbol (best-effort)
        # (simple + stable; avoids storing regime per label)
        from collections import defaultdict

        by_reg = defaultdict(list)   # (sym,h,reg) -> [z]
        by_glob = defaultdict(list)  # (sym,h) -> [z]

        symset = set(str(s) for s in (symbols or []))
        hset = set(int(h) for h in (horizons or []))

        for sym, h, z in rows or []:
            sym = str(sym)
            h = int(h)
            if symset and sym not in symset:
                continue
            if hset and h not in hset:
                continue
            try:
                zz = float(z)
            except Exception:
                continue

            reg = get_current_regime(sym)
            by_reg[(sym, h, reg)].append(zz)
            by_glob[(sym, h)].append(zz)

        cur = con.cursor()
        updated = 0

        # upsert regime stats
        for (sym, h, reg), zs in by_reg.items():
            n = int(len(zs))
            if n <= 0:
                continue
            mean_z = float(sum(zs) / n)
            cur.execute(
                """
                INSERT INTO model_stats_regime(ts_ms, symbol, horizon_s, regime, n, mean_impact_z)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, horizon_s, regime) DO UPDATE SET
                  ts_ms=excluded.ts_ms,
                  n=excluded.n,
                  mean_impact_z=excluded.mean_impact_z
                """,
                (int(now_ms), str(sym), int(h), str(reg), int(n), float(mean_z)),
            )
            updated += 1

        # upsert global stats
        for (sym, h), zs in by_glob.items():
            n = int(len(zs))
            if n <= 0:
                continue
            mean_z = float(sum(zs) / n)
            cur.execute(
                """
                INSERT INTO model_stats(ts_ms, symbol, horizon_s, n, mean_impact_z)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(symbol, horizon_s) DO UPDATE SET
                  ts_ms=excluded.ts_ms,
                  n=excluded.n,
                  mean_impact_z=excluded.mean_impact_z
                """,
                (int(now_ms), str(sym), int(h), int(n), float(mean_z)),
            )
            updated += 1

        con.commit()
        return int(updated)
    finally:
        con.close()
