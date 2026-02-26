# dev_core/size_policy.py
import json
from typing import Optional, Dict, Any, List

from engine.runtime.storage import connect, init_db


def load_latest_size_policy(con=None) -> Optional[Dict[str, Any]]:
    """
    Returns dict:
      {policy_id, ts_ms, method, params, metrics, points:[{conf_lo,conf_hi,factor,...}]}
    """
    init_db()
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        r = con.execute(
            """
            SELECT id, ts_ms, method, params_json, metrics_json
            FROM size_policy
            ORDER BY ts_ms DESC
            LIMIT 1
            """
        ).fetchone()
        if not r:
            return None
        pid, ts_ms, method, pj, mj = r
        params = json.loads(pj or "{}")
        metrics = json.loads(mj or "{}")

        pts = con.execute(
            """
            SELECT bucket_idx, conf_lo, conf_hi, n, mean_net_ret, std_net_ret, factor
            FROM size_policy_points
            WHERE policy_id=?
            ORDER BY bucket_idx ASC
            """,
            (int(pid),),
        ).fetchall()

        points: List[Dict[str, Any]] = []
        for bi, clo, chi, n, mnr, sdr, f in pts or []:
            points.append({
                "bucket_idx": int(bi),
                "conf_lo": float(clo),
                "conf_hi": float(chi),
                "n": int(n),
                "mean_net_ret": float(mnr),
                "std_net_ret": float(sdr),
                "factor": float(f),
            })

        return {
            "policy_id": int(pid),
            "ts_ms": int(ts_ms),
            "method": str(method),
            "params": params,
            "metrics": metrics,
            "points": points,
        }
    finally:
        if owns:
            con.close()


def size_factor(policy: Optional[Dict[str, Any]], conf: float, drawdown: float = 0.0) -> float:
    """
    Map confidence -> [0..1] factor using latest learned buckets.
    Optionally multiply by dd_factor(drawdown) if present in policy params.

    Falls back to 1.0 if no policy exists.
    """
    if policy is None:
        return 1.0

    try:
        c = float(conf)
    except Exception:
        c = 0.0
    if c <= 0:
        base = 0.0
    else:
        pts = policy.get("points") or []
        base = None
        for p in pts:
            if c >= float(p["conf_lo"]) and c < float(p["conf_hi"]):
                base = float(p.get("factor", 1.0))
                break
        if base is None:
            base = float(pts[-1].get("factor", 1.0)) if pts else 1.0
        base = max(0.0, min(1.0, float(base)))

    # dd_factor from params if present
    dd_mult = 1.0
    try:
        dd = max(0.0, min(1.0, float(drawdown or 0.0)))
    except Exception:
        dd = 0.0

    try:
        params = policy.get("params") or {}
        dd_points = params.get("dd_points") or []
        for p in dd_points:
            lo = float(p.get("dd_lo", 0.0))
            hi = float(p.get("dd_hi", 1.0))
            if dd >= lo and dd < hi:
                dd_mult = float(p.get("factor", 1.0))
                break
        if dd_points and dd >= float(dd_points[-1].get("dd_lo", 0.0)):
            dd_mult = float(dd_points[-1].get("factor", dd_mult))
        dd_mult = max(0.0, min(1.0, float(dd_mult)))
    except Exception:
        dd_mult = 1.0

    return max(0.0, min(1.0, float(base) * float(dd_mult)))
