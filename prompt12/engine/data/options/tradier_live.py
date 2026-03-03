import os
import time
import requests

_TRADIER_TOKEN = os.environ.get("TRADIER_API_TOKEN")
_BASE = "https://api.tradier.com/v1"

_HEADERS = {
    "Authorization": f"Bearer {_TRADIER_TOKEN}",
    "Accept": "application/json",
}


def fetch_options_chain(symbol: str):
    """
    Returns list of:
      { expiry, strike, call_put, iv, open_interest, volume }
    """
    if not _TRADIER_TOKEN:
        return []

    out = []
    sym = symbol.upper()

    # expirations
    r = requests.get(
        f"{_BASE}/markets/options/expirations",
        params={"symbol": sym},
        headers=_HEADERS,
        timeout=8,
    )
    r.raise_for_status()
    expiries = r.json().get("expirations", {}).get("date", [])

    for exp in expiries[:3]:  # nearest expiries only (profit-focused)
        rc = requests.get(
            f"{_BASE}/markets/options/chains",
            params={
                "symbol": sym,
                "expiration": exp,
                "greeks": "true",
            },
            headers=_HEADERS,
            timeout=8,
        )
        rc.raise_for_status()
        opts = rc.json().get("options", {}).get("option", [])
        for o in opts or []:
            try:
                out.append(
                    {
                        "expiry": exp,
                        "strike": float(o.get("strike")),
                        "call_put": str(o.get("option_type")).upper(),  # C / P
                        "iv": float(o.get("implied_volatility")) if o.get("implied_volatility") is not None else None,
                        "open_interest": int(o.get("open_interest")) if o.get("open_interest") is not None else None,
                        "volume": int(o.get("volume")) if o.get("volume") is not None else None,
                    }
                )
            except Exception:
                continue

    return out
