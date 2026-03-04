import os
import requests

FMP_KEY = os.environ.get("FMP_API_KEY")
BASE = "https://financialmodelingprep.com/api/v3"


def fetch_earnings_calendar(from_date: str, to_date: str) -> list:
    """
    Returns list of dicts:
      { symbol, date, time, epsEstimated, eps, revenueEstimated, revenue }
    """
    if not FMP_KEY:
        return []

    r = requests.get(
        f"{BASE}/earning_calendar",
        params={"from": from_date, "to": to_date, "apikey": FMP_KEY},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    return j if isinstance(j, list) else []
