import os
import json
import time
import requests
from pathlib import Path

SEC_UA = os.environ.get("SEC_USER_AGENT", "market-impact-dev (contact: ops@example.com)")
SEC_FROM = os.environ.get("SEC_FROM")  # optional but recommended by SEC guidance

HEADERS = {"User-Agent": SEC_UA}
if SEC_FROM:
    HEADERS["From"] = SEC_FROM

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

CACHE_PATH = Path(os.environ.get("SEC_TICKER_MAP_CACHE", "data/sec_company_tickers_exchange.json"))
CACHE_MAX_AGE_S = int(os.environ.get("SEC_TICKER_MAP_MAX_AGE_S", str(24 * 3600)))


def _download_ticker_map() -> dict:
    r = requests.get(TICKER_MAP_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def _load_ticker_map() -> dict:
    try:
        if CACHE_PATH.exists():
            age = time.time() - CACHE_PATH.stat().st_mtime
            if age <= CACHE_MAX_AGE_S:
                return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass

    mp = _download_ticker_map()
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(mp), encoding="utf-8")
    except Exception:
        pass
    return mp


def ticker_to_cik(ticker: str) -> str:
    """
    Returns zero-padded 10-digit CIK string, or "" if not found.
    """
    t = (ticker or "").upper().strip()
    if not t:
        return ""

    mp = _load_ticker_map()

    # company_tickers_exchange.json format: list of objects (as of SEC docs),
    # but sometimes appears as dict keyed by index. Handle both.
    try:
        if isinstance(mp, list):
            rows = mp
        elif isinstance(mp, dict):
            rows = list(mp.values())
        else:
            rows = []
    except Exception:
        rows = []

    for r in rows:
        try:
            if str(r.get("ticker", "")).upper() == t:
                cik = str(r.get("cik", "") or r.get("cik_str", "")).strip()
                if not cik:
                    continue
                return cik.zfill(10)
        except Exception:
            continue
    return ""


def fetch_recent_filings(ticker: str, limit: int = 25) -> list:
    """
    Returns list of dicts:
      { accession, form, filed_date, report_date, cik, company_name, primary_doc_url }
    """
    cik = ticker_to_cik(ticker)
    if not cik:
        return []

    url = SUBMISSIONS_URL.format(cik=cik)
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    j = r.json() or {}

    company_name = j.get("name")
    recent = (j.get("filings") or {}).get("recent") or {}

    forms = recent.get("form") or []
    accs = recent.get("accessionNumber") or []
    fdates = recent.get("filingDate") or []
    rdates = recent.get("reportDate") or []
    prim_docs = recent.get("primaryDocument") or []

    out = []
    n = min(len(forms), len(accs), len(fdates), len(prim_docs))
    for i in range(n):
        try:
            acc = str(accs[i])
            form = str(forms[i])
            fd = str(fdates[i])
            rd = str(rdates[i]) if i < len(rdates) and rdates[i] else None
            primary = str(prim_docs[i])

            # construct primary doc URL (standard SEC archives path)
            acc_nodash = acc.replace("-", "")
            primary_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_nodash}/{primary}"

            out.append(
                {
                    "accession": acc,
                    "form": form,
                    "filed_date": fd,
                    "report_date": rd,
                    "cik": cik,
                    "company_name": company_name,
                    "primary_doc_url": primary_url,
                }
            )
        except Exception:
            continue

        if len(out) >= int(limit):
            break

    return out
