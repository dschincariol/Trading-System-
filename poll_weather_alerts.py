# CREATE NEW FILE: poll_weather_alerts.py
# (copy/paste entire file)

# poll_weather_alerts.py
"""
Weather alerts poller (event stream).

Provider: NWS (api.weather.gov)
Stores alerts into weather_alerts with affected_regions mapped from WEATHER_REGION_MAP.

Config:
- WEATHER_REGION_MAP=data/weather_region_map.json
- WEATHER_ALERTS_POLL_SECONDS=900 (15m)
- WEATHER_ALERTS_PROVIDER=nws

Region map supports optional NWS filter:
{
  "regions": {
    "us_gulf": {"nws_area": "TX"}   // 2-letter US state/area code
  }
}
"""

import os
import time
import json
import logging
from typing import Dict, Any, List

import requests

from dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

_REGION_MAP_CACHE = None

WEATHER_ALERTS_PROVIDER = os.environ.get("WEATHER_ALERTS_PROVIDER", "nws").strip().lower()
WEATHER_REGION_MAP = os.environ.get("WEATHER_REGION_MAP", os.path.join("data", "weather_region_map.json"))

POLL_SECONDS = int(os.environ.get("WEATHER_ALERTS_POLL_SECONDS", "900"))  # 15m
JOB_NAME = "poll_weather_alerts"
OWNER = os.environ.get("JOB_OWNER", os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")))
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "300"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "30.0"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [poll_weather_alerts] %(message)s",
)

UA = os.environ.get("WEATHER_HTTP_UA", "trading-system/1.0 (admin@example.com)")


def _load_region_map() -> Dict[str, Any]:
    global _REGION_MAP_CACHE
    if _REGION_MAP_CACHE is not None:
        return _REGION_MAP_CACHE
    try:
        with open(WEATHER_REGION_MAP, "r", encoding="utf-8") as f:
            _REGION_MAP_CACHE = json.load(f) or {}
            return _REGION_MAP_CACHE
    except Exception:
        return {}


def _fetch_nws_active(area: str) -> Dict[str, Any]:
    url = "https://api.weather.gov/alerts/active"
    params = {}
    if area:
        params["area"] = str(area).upper()
    headers = {"User-Agent": UA, "Accept": "application/geo+json"}
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json() or {}


def _ts_to_ms(ts: Any) -> int:
    # NWS timestamps are ISO8601
    try:
        s = str(ts).strip()
        if not s:
            return 0
        # Handle trailing 'Z' for UTC
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dtv = __import__("datetime").datetime.fromisoformat(s)
        if dtv.tzinfo is None:
            dtv = dtv.replace(tzinfo=__import__("datetime").timezone.utc)
        return int(dtv.timestamp() * 1000)
    except Exception:
        return 0


def _upsert_alert(con, provider: str, alert_id: str, issued_ts: int, effective_ts: int, expires_ts: int,
                  event: str, severity: str, urgency: str, certainty: str,
                  area_desc: str, polygon_geojson: str, affected_regions_json: str,
                  headline: str, description: str, source_uri: str) -> None:
    con.execute(
        """
        INSERT OR IGNORE INTO weather_alerts(
          provider, alert_id, issued_ts, effective_ts, expires_ts,
          event, severity, urgency, certainty,
          area_desc, polygon_geojson, affected_regions,
          headline, description, source_uri
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(provider),
            str(alert_id),
            int(issued_ts),
            int(effective_ts) if effective_ts else None,
            int(expires_ts) if expires_ts else None,
            str(event) if event else None,
            str(severity) if severity else None,
            str(urgency) if urgency else None,
            str(certainty) if certainty else None,
            str(area_desc) if area_desc else None,
            polygon_geojson,
            affected_regions_json,
            str(headline) if headline else None,
            str(description) if description else None,
            str(source_uri) if source_uri else None,
        ),
    )


def _run_once() -> None:
    cfg = _load_region_map()
    regions = (cfg or {}).get("regions") or {}
    if not isinstance(regions, dict) or not regions:
        logging.info("no regions configured in WEATHER_REGION_MAP; nothing to do")
        return

    # Group region_ids by nws_area for efficient queries
    area_to_regions: Dict[str, List[str]] = {}
    for rid, meta in regions.items():
        area = str((meta or {}).get("nws_area") or "").strip().upper()
        if not area:
            continue
        area_to_regions.setdefault(area, []).append(str(rid))

    if not area_to_regions:
        logging.info("no regions have nws_area configured; nothing to do")
        return

    put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"phase": "fetch", "provider": WEATHER_ALERTS_PROVIDER}))

    con = connect()
    try:
        for area, region_ids in area_to_regions.items():
            try:
                touch_job_lock(JOB_NAME, OWNER, PID)
                put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"phase": "area", "area": str(area)}))
            except Exception:
                pass
            try:
                if WEATHER_ALERTS_PROVIDER != "nws":
                    raise RuntimeError(f"unsupported WEATHER_ALERTS_PROVIDER={WEATHER_ALERTS_PROVIDER}")
                payload = _fetch_nws_active(area)
                feats = payload.get("features") or []
                n = 0

                for f in feats:
                    props = (f or {}).get("properties") or {}
                    aid = props.get("id") or props.get("@id") or props.get("api") or ""
                    aid = str(aid).strip()
                    if not aid:
                        continue
                    # normalize: if it's a URL, keep the last path segment as stable id
                    if "://" in aid and "/" in aid:
                        try:
                            aid = aid.rstrip("/").split("/")[-1]
                        except Exception:
                            pass

                    issued_ms = _ts_to_ms(props.get("sent") or props.get("issued") or props.get("onset") or props.get("effective"))
                    effective_ms = _ts_to_ms(props.get("effective") or props.get("onset"))
                    expires_ms = _ts_to_ms(props.get("expires"))

                    _upsert_alert(
                        con,
                        provider=WEATHER_ALERTS_PROVIDER,
                        alert_id=aid,
                        issued_ts=int(issued_ms) if issued_ms else int(time.time() * 1000),
                        effective_ts=int(effective_ms) if effective_ms else 0,
                        expires_ts=int(expires_ms) if expires_ms else 0,
                        event=str(props.get("event") or ""),
                        severity=str(props.get("severity") or ""),
                        urgency=str(props.get("urgency") or ""),
                        certainty=str(props.get("certainty") or ""),
                        area_desc=str(props.get("areaDesc") or ""),
                        polygon_geojson=json.dumps((f or {}).get("geometry")) if (f or {}).get("geometry") is not None else None,
                        affected_regions_json=json.dumps(region_ids),
                        headline=str(props.get("headline") or ""),
                        description=str(props.get("description") or ""),
                        source_uri=str(props.get("@id") or ""),
                    )
                    n += 1

                con.commit()
                logging.info(f"ingested nws alerts area={area} n={n}")

            except Exception as e:
                logging.warning(f"area={area} alerts fetch failed: {e}")

    finally:
        con.close()


def main() -> None:
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=LOCK_STALE_AFTER_S):
        logging.info("job locked by another instance; exiting")
        return

    try:
        last_hb = 0.0
        while True:
            now_s = time.time()
            if now_s - last_hb >= HEARTBEAT_EVERY_S:
                touch_job_lock(JOB_NAME, OWNER, PID)
                put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"phase": "loop"}))
                last_hb = now_s

            _run_once()
            time.sleep(max(30, int(POLL_SECONDS)))

    finally:
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
