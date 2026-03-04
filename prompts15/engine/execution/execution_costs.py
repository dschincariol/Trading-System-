# dev_core/execution_costs.py
import os
from typing import Dict, Any, Optional

from engine.horizons import get_spec

try:
    from engine.storage import connect  # type: ignore
except Exception:
    connect = None  # type: ignore

DEFAULT_FEES_BPS = float(os.environ.get("EXEC_FEES_BPS", "0.5"))          # commission/fees
DEFAULT_SLIPPAGE_BPS = float(os.environ.get("EXEC_SLIPPAGE_BPS", "2.0"))  # impact/queue/slip
DEFAULT_SPREAD_BPS_CAP = float(os.environ.get("EXEC_SPREAD_BPS_CAP", "30.0"))

def _bps(x: float) -> float:
    return float(x) * 1e4

def estimate_cost_bps(
    *,
    px: float,
    bid: Optional[float],
    ask: Optional[float],
    side: int,
    horizon_s: Optional[int] = None,
    asset_class: Optional[str] = None,
    fees_bps: float = DEFAULT_FEES_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    symbol: Optional[str] = None,
    venue: Optional[str] = None,
    instrument_type: Optional[str] = None,
    volatility: Optional[float] = None,
    spread_bps: Optional[float] = None,
    order_qty: Optional[float] = None,
    con=None,
) -> Dict[str, float]:
    """
    Cost model (bps):
    - spread_bps: if bid/ask known => half-spread as expected crossing cost (entry only)
    - slippage_bps: fixed additional penalty (models impact + queue + latency)
    - fees_bps: fixed commission/fees

    Returned:
      spread_bps, slippage_bps, fees_bps, total_cost_bps
    """
    # -----------------------------------------------------------------
    # Learned cost model (optional, live adaptive)
    # -----------------------------------------------------------------
    # If symbol is provided and learned_cost_model exists, prefer it.
    # We bucket (volatility, spread_bps, order_qty) and lookup a conditional mean.
    # Fail-soft: any DB/schema issue reverts to existing static behavior.
    sym = str(symbol or "").upper().strip() if symbol is not None else ""
    vnu = str(venue or "").upper().strip() if venue is not None else None
    it = str(instrument_type or "").upper().strip() if instrument_type is not None else None

    owns = False
    if con is None and connect is not None:
        try:
            con = connect(readonly=True)
            owns = True
        except Exception:
            con = None

    if con is not None and sym:
        try:
            chk = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='learned_cost_model'"
            ).fetchone()
        except Exception:
            chk = None

        if chk:
            vb = None
            lb = None
            qb = None
            try:
                x = float(volatility or 0.0)
                if x <= 0:
                    vb = "v_0"
                elif x < 0.01:
                    vb = "v_1"
                elif x < 0.02:
                    vb = "v_2"
                elif x < 0.035:
                    vb = "v_3"
                else:
                    vb = "v_4"
            except Exception:
                vb = "v_unk"

            try:
                sb = float(spread_bps) if spread_bps is not None else 0.0
                if sb <= 0:
                    lb = "s_0"
                elif sb < 2.0:
                    lb = "s_1"
                elif sb < 5.0:
                    lb = "s_2"
                elif sb < 12.0:
                    lb = "s_3"
                else:
                    lb = "s_4"
            except Exception:
                lb = "s_unk"

            try:
                q = abs(float(order_qty or 0.0))
                if q <= 0:
                    qb = "q_0"
                elif q <= 1:
                    qb = "q_1"
                elif q <= 10:
                    qb = "q_2"
                elif q <= 100:
                    qb = "q_3"
                elif q <= 1000:
                    qb = "q_4"
                else:
                    qb = "q_5"
            except Exception:
                qb = "q_unk"

            # Prefer fully keyed row; otherwise back off by dropping venue/instrument_type.
            # We also estimate uncertainty via m2/n.
            row = None
            try:
                row = con.execute(
                    """
                    SELECT n, mean_realized_cost_bps, m2_realized_cost_bps
                    FROM learned_cost_model
                    WHERE symbol=?
                      AND vol_bucket=?
                      AND liq_bucket=?
                      AND size_bucket=?
                      AND (? IS NULL OR venue=? )
                      AND (? IS NULL OR instrument_type=? )
                    ORDER BY last_update_ts_ms DESC
                    LIMIT 1
                    """,
                    (sym, str(vb), str(lb), str(qb), vnu, vnu, it, it),
                ).fetchone()
            except Exception:
                row = None

            if row:
                try:
                    n = int(row[0] or 0)
                    mu = float(row[1] or 0.0)
                    m2 = float(row[2] or 0.0)
                except Exception:
                    n = 0
                    mu = 0.0
                    m2 = 0.0

                if n >= int(os.environ.get("EXEC_LEARNED_MIN_N", "25")):
                    # Use learned mean as total cost; decompose into spread/fees + residual slippage.
                    spread_bps_eff = float(spread_bps or 0.0)
                    fees_bps_eff = float(fees_bps)
                    total = float(mu)
                    residual = float(max(0.0, total - spread_bps_eff - fees_bps_eff))

                    # simple variance estimate (sample variance = m2/(n-1))
                    var = float(m2) / float(max(1, n - 1)) if n > 1 else 0.0
                    std = float(var ** 0.5) if var > 0 else 0.0

                    if owns:
                        try:
                            con.close()
                        except Exception:
                            pass

                    return {
                        "spread_bps": float(max(0.0, spread_bps_eff)),
                        "slippage_bps": float(max(0.0, residual)),
                        "fees_bps": float(max(0.0, fees_bps_eff)),
                        "total_cost_bps": float(max(0.0, total)),
                        "cost_std_bps": float(max(0.0, std)),
                        "cost_n": float(n),
                    }

    if fees_bps == DEFAULT_FEES_BPS or slippage_bps == DEFAULT_SLIPPAGE_BPS:
        try:
            spec = get_spec(int(horizon_s)) if horizon_s is not None else None
        except Exception:
            spec = None

        if spec is not None:
            if fees_bps == DEFAULT_FEES_BPS:
                fees_bps = float(spec.cost_fees_bps)
            if slippage_bps == DEFAULT_SLIPPAGE_BPS:
                slippage_bps = float(spec.cost_slippage_bps)

    spread_bps = 0.0
    if bid is not None and ask is not None:
        try:
            spr = max(0.0, float(ask) - float(bid))
            if px > 0:
                # expected crossing cost approx half spread (entry)
                spread_bps = min(DEFAULT_SPREAD_BPS_CAP, _bps(0.5 * spr / float(px)))
        except Exception:
            spread_bps = 0.0

    total = float(fees_bps) + float(slippage_bps) + float(spread_bps)
    if owns:
        try:
            con.close()
        except Exception:
            pass
    return {
        "spread_bps": float(spread_bps),
        "slippage_bps": float(slippage_bps),
        "fees_bps": float(fees_bps),
        "total_cost_bps": float(total),
        "cost_std_bps": 0.0,
        "cost_n": 0.0,
    }

def apply_cost_to_return(gross_ret: float, total_cost_bps: float, side: int) -> float:
    """
    Convert gross return to net by subtracting costs in direction-of-trade terms.
    Costs always reduce P&L, so subtract absolute bps.
    """
    # returns are in decimal (e.g., 0.001 = 10 bps)
    cost = float(total_cost_bps) / 1e4
    return float(gross_ret) - cost
