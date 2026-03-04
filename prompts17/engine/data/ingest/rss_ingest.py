# dev_core/ingest/rss_ingest.py
import time
import hashlib
import random
import logging
import calendar
from typing import List, Dict, Any, Optional, Tuple
import json
import re
import requests
import feedparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ------            -- ------------------------------------------------------
# Event enrichment helpers (additive, non-breaking)
# ------            -- ------------------------------------------------------

_TAXONOMY_RULES = {
    "crypto": r"\b(bitcoin|btc|ethereum|eth|crypto|blockchain|defi|stablecoin)\b",
    "macro": r"\b(fed|fomc|rates?|inflation|cpi|pce|gdp|jobs?|payrolls?)\b",
    "energy": r"\b(oil|crude|wti|brent|opec|energy|gasoline)\b",
    "earnings": r"\b(earnings|guidance|revenue|profit|loss)\b",
    "geopolitics": r"\b(war|sanction|military|conflict|iran|russia|china|ukraine)\b",
}

_ENTITY_RX = re.compile(r"\b[A-Z]{2,6}\b")


def _extract_taxonomy(text: str):
    tags = []
    t = (text or "").lower()
    for name, pat in _TAXONOMY_RULES.items():
        try:
            if re.search(pat, t, flags=re.IGNORECASE):
                tags.append(name)
        except re.error:
            continue
    return tags

from engine.data.asset_map import asset_class_for_symbol
# import source blocking helper to tag incoming events
from engine.strategy.news_domain import is_source_blocked


def _extract_entities(text: str):
    if not text:
        return []
    return sorted(set(_ENTITY_RX.findall(text)))


def _normalize_entities(text: str):
    """Return list of normalized entity dicts extracted from free text.

    Each entry is ``{"raw":str, "symbol":str, "asset_class":str}``.
    If the uppercase token does not look like a known symbol we still return it
    with ``asset_class='UNKNOWN'`` so downstream tools can filter later.
    """
    ents = _extract_entities(text)
    out = []
    for raw in ents:
        sym = str(raw).upper().strip()
        asset_cls = asset_class_for_symbol(sym)
        out.append({"raw": raw, "symbol": sym, "asset_class": asset_cls})
    return out

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _clean(s: Optional[str]) -> str:
    return (s or "").strip()


def _sleep_jitter(s: float) -> None:
    if s <= 0:
        return
    j = s * 0.2
    time.sleep(max(0.05, s + random.uniform(-j, j)))


def _make_session() -> requests.Session:
    sess = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    return sess


_SESSION = _make_session()


def fetch_rss(url: str, timeout_s: int = 15, max_attempts: int = 3) -> feedparser.FeedParserDict:
    headers = {"User-Agent": "market-impact-dev/1.0 (+rss)"}

    last_err: Optional[Exception] = None
    for attempt in range(1, int(max_attempts) + 1):
        try:
            r = _SESSION.get(url, headers=headers, timeout=timeout_s)
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code} {r.reason}", response=r)
            return feedparser.parse(r.content)
        except Exception as e:
            last_err = e
            base = min(20.0, 1.5 ** max(0, attempt - 1))
            _sleep_jitter(base)
    assert last_err is not None
    raise last_err


def ingest_rss_sources(
    sources: List[Dict[str, Any]],
    max_items_per_source: int = 25,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Returns (items, errors)

    items:
      { ts_ms, source, title, body, url, event_key }

    errors:
      { source_name, url, error }
    """
    out: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    now_ms = int(time.time() * 1000)

    for src in sources or []:
        name = _clean(src.get("name")) or "rss"
        url = _clean(src.get("url"))
        if not url:
            errors.append({"source_name": name, "url": url, "error": "missing_url"})
            continue

        try:
            feed = fetch_rss(url)
        except Exception as e:
            logging.warning("[rss_ingest] fetch failed name=%s url=%s err=%r", name, url, e)
            errors.append({"source_name": name, "url": url, "error": str(e)})
            continue

        entries = list(feed.entries or [])[:max_items_per_source]
        for ent in entries:
            title = _clean(getattr(ent, "title", "") or ent.get("title"))
            link = _clean(getattr(ent, "link", "") or ent.get("link"))
            summary = _clean(getattr(ent, "summary", "") or ent.get("summary"))

            # Some feeds store the full text here
            try:
                if not summary:
                    content = getattr(ent, "content", None) or ent.get("content")
                    if content and isinstance(content, list) and content:
                        summary = _clean(getattr(content[0], "value", "") or content[0].get("value"))
            except Exception:
                pass

            if not title and not summary and not link:
                continue

            ts_ms = now_ms
            try:
                pp = getattr(ent, "published_parsed", None) or ent.get("published_parsed")
                up = getattr(ent, "updated_parsed", None) or ent.get("updated_parsed")

                # feedparser returns time.struct_time; treat it as UTC
                if pp:
                    ts_ms = int(calendar.timegm(pp) * 1000)
                elif up:
                    ts_ms = int(calendar.timegm(up) * 1000)
            except Exception:
                ts_ms = now_ms

            base = link or (title + "|" + summary)
            event_key = f"rss:{name}:{_hash(base)}"

            text_blob = f"{title}\n{summary}"

            meta = {
                "taxonomy": _extract_taxonomy(text_blob),
                "entities": _normalize_entities(text_blob),
                "novelty": None,  # computed later once embeddings exist
                "ingest_source": "rss",
                "source_name": name,
            }
            # if the feed itself has been blacklisted globally we still add the
            # row to the DB but mark it so later stages can easily quarantine it.
            if is_source_blocked(f"rss:{name}", "*"):
                meta["quarantine"] = True

            out.append(
                {
                    "ts_ms": int(ts_ms),
                    "source": f"rss:{name}",
                    "title": title,
                    "body": summary,
                    "url": link,
                    "event_key": event_key,
                    "meta_json": json.dumps(meta, separators=(",", ":"), sort_keys=True),
                }
            )

    out.sort(key=lambda x: x["ts_ms"], reverse=True)
    return out, errors
