# dev_core/risk.py
"""
Risk helpers (opt-in).
Vol targeting scales target weights by recent realized vol from prices table.

Env:
  PORTFOLIO_USE_VOL_TARGET=1
  PORTFOLIO_VOL_LOOKBACK=240       # number of price points
  PORTFOLIO_TARGET_VOL=0.020       # target stdev of returns (per-step)
  PORTFOLIO_VOL_FLOOR=0.005
  PORTFOLIO_VOL_CEIL=0.080
"""

import math
import os

PORTFOLIO_USE_VOL_TARGET = os.environ.get("PORTFOLIO_USE_VOL_TARGET", "0") == "1"
VOL_LOOKBACK = int(os.environ.get("PORTFOLIO_VOL_LOOKBACK", "240"))
TARGET_VOL = float(os.environ.get("PORTFOLIO_TARGET_VOL", "0.020"))
VOL_FLOOR = float(os.environ.get("PORTFOLIO_VOL_FLOOR", "0.005"))
VOL_CEIL = float(os.environ.get("PORTFOLIO_VOL_CEIL", "0.080"))

def _stdev(xs):
  n = len(xs)
  if n < 3: return None
  m = sum(xs) / n
  v = sum((x - m) * (x - m) for x in xs) / (n - 1)
  return math.sqrt(max(0.0, v))

def realized_vol_from_prices(con, symbol: str, lookback: int = VOL_LOOKBACK, ts_ms: int = None):
  # restrict to prices before ts_ms when supplied
  if ts_ms is None:
    rows = con.execute(
      """
      SELECT price
      FROM prices
      WHERE symbol = ?
      ORDER BY ts_ms DESC
      LIMIT ?
      """,
      (str(symbol), int(lookback)),
    ).fetchall()
  else:
    rows = con.execute(
      """
      SELECT price
      FROM prices
      WHERE symbol = ? AND ts_ms < ?
      ORDER BY ts_ms DESC
      LIMIT ?
      """,
      (str(symbol), int(ts_ms), int(lookback)),
    ).fetchall()
  px = [float(r[0]) for r in rows if r and r[0] is not None]
  px.reverse()
  if len(px) < 4:
    return None

  rets = []
  for i in range(1, len(px)):
    if px[i-1] > 0 and px[i] > 0:
      rets.append(math.log(px[i] / px[i-1]))
  v = _stdev(rets)
  if v is None:
    return None
  return max(VOL_FLOOR, min(VOL_CEIL, float(v)))

def vol_scale_weight(weight: float, vol: float):
  # scale so higher vol => smaller weight
  if not vol or vol <= 0:
    return float(weight)
  m = float(TARGET_VOL / float(vol))
  return float(weight) * float(m)

def _logrets_from_prices(con, symbol: str, lookback: int = 240, ts_ms: int = None):
  if ts_ms is None:
    rows = con.execute(
      """
      SELECT price
      FROM prices
      WHERE symbol = ?
      ORDER BY ts_ms DESC
      LIMIT ?
      """,
      (str(symbol), int(lookback)),
    ).fetchall()
  else:
    rows = con.execute(
      """
      SELECT price
      FROM prices
      WHERE symbol = ? AND ts_ms < ?
      ORDER BY ts_ms DESC
      LIMIT ?
      """,
      (str(symbol), int(ts_ms), int(lookback)),
    ).fetchall()
  px = [float(r[0]) for r in rows if r and r[0] is not None]
  px.reverse()
  if len(px) < 6:
    return None

  rets = []
  for i in range(1, len(px)):
    if px[i-1] > 0 and px[i] > 0:
      rets.append(math.log(px[i] / px[i-1]))
  if len(rets) < 5:
    return None
  return rets

def corr_from_prices(con, a: str, b: str, lookback: int = 240, ts_ms: int = None):
  ra = _logrets_from_prices(con, a, lookback=lookback, ts_ms=ts_ms)
  rb = _logrets_from_prices(con, b, lookback=lookback, ts_ms=ts_ms)
  if not ra or not rb:
    return None

  n = min(len(ra), len(rb))
  if n < 6:
    return None

  xa = ra[-n:]
  xb = rb[-n:]
  ma = sum(xa) / n
  mb = sum(xb) / n
  va = sum((x - ma) * (x - ma) for x in xa)
  vb = sum((x - mb) * (x - mb) for x in xb)
  if va <= 1e-12 or vb <= 1e-12:
    return None

  cov = sum((xa[i] - ma) * (xb[i] - mb) for i in range(n))
  return float(cov / math.sqrt(va * vb))
