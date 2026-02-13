import time
import json
from urllib.parse import urlparse

from dev_core.storage import connect
from dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot


def extract_domain(url: str, meta_json: str = None) -> str:
    # prefer structured provider metadata
    try:
        if meta_json:
            m = json.loads(meta_json)
            if isinstance(m, dict):
                gd = m.get("gdelt")
                if isinstance(gd, dict) and gd.get("domain"):
                    return str(gd.get("domain")).lower().strip()
    except Exception:
        pass

    try:
        u = (url or "").strip()
        if not u:
            return ""
        host = urlparse(u).netloc.lower().strip()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def is_domain_blocked(domain: str, symbol: str) -> bool:
    d = (domain or "").lower().strip()
    s = (symbol or "").upper().strip()
    if not d or not s:
        return False

    con = connect()
    try:
        # symbol-specific rule wins, then global '*'
        row = con.execute(
            """
            SELECT status
            FROM domain_blacklist
            WHERE domain=? AND symbol IN (?, '*')
            ORDER BY CASE WHEN symbol=? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (d, s, s),
        ).fetchone()
        if not row:
            return False
        return str(row[0] or "").upper() == "BLOCK"
    except Exception:
        return False
    finally:
        con.close()


def domain_conf_multiplier(domain: str, symbol: str, regime: str, horizon_s: int) -> float:
    d = (domain or "").lower().strip()
    s = (symbol or "").upper().strip()
    r = (regime or "MID").upper().strip()
    h = int(horizon_s or 0)
    if not d or not s or h <= 0:
        return 1.0

    con = connect()
    try:
        row = con.execute(
            """
            SELECT mean_edge, win_rate, n
            FROM domain_perf
            WHERE domain=? AND symbol=? AND regime=? AND horizon_s=?
            """,
            (d, s, r, h),
        ).fetchone()
        if not row:
            return 1.0

        mean_edge = row[0]
        win_rate = row[1]
        n = int(row[2] or 0)
        if n < 30:
            return 1.0

        # Simple stable mapping:
        # - if mean_edge negative => downweight
        # - if win_rate low => downweight
        mult = 1.0
        try:
            if mean_edge is not None and float(mean_edge) < 0.0:
                mult *= 0.85
        except Exception:
            pass
        try:
            if win_rate is not None and float(win_rate) < 0.45:
                mult *= 0.90
        except Exception:
            pass

        return float(max(0.50, min(1.10, mult)))
    except Exception:
        return 1.0
    finally:
        con.close()
