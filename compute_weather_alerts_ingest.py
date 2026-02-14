# CREATE NEW FILE: compute_weather_alerts_ingest.py
"""
Weather alerts ingestion job (NWS active alerts).
Stores into `weather_alerts` (leakage-safe event stream).

Provider: 'nws'
Source: api.weather.gov/alerts/active
"""

import os
import time
import json
import logging
import urllib.request
from typing import Dict, Any, List, Optional

from engine.dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

LOG = logging.getLogger("compute_weather_alerts_ingest")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

JOB_NAME = "compute_weather_alerts_ingest"
OWNER = os.environ.get("JOB_OWNER", "system")
PID = os.getpid()

PROVIDER = "nws"
POLL_INTERVAL_S = int(os.environ.get("WEATHER_ALERTS_INTERVAL_S", "600"))  # 10 min
LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "300"))

# Optional: if set, only alerts that match any of these codes are kept
# Example: "CA,TX,FL"
NWS_AREAS = [s.strip().upper() for s in os.environ.get("NWS_ALERT_AREAS", "").split(",") if s.strip()]


def _utc_ms() -> int:
    return int(time.time() * 1000)


def _http_json(url: str, timeout: int = 20) -> Optional[Dict[str, Any]]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "weather-alerts-ingest/1.0",
            "Accept": "application/geo+json,application/json;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if getattr(r, "status", 200) != 200:
            return None
        return json.loads(r.read().decode("utf-8"))


def _iso_to_ms(s: Optional[str]) -> int:
    if not s:
        return 0
    try:
        # example: 2026-02-05T21:16:00-06:00
        # Use fromisoformat (py3.11 ok) then convert to epoch
        import datetime as _dt
        dt = _dt.datetime.fromisoformat(str(s))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _poly_to_geojson(geom: Any) -> Optional[str]:
    try:
        if geom is None:
            return None
        return json.dumps(geom, separators=(",", ":"), sort_keys=True)
    except Exception:
        return None


def _normalize_regions_from_area_desc(area_desc: str, region_map: Dict[str, Any]) -> List[str]:
    """
    Best-effort mapping: if your region map defines regions with keywords,
    match those keywords in area_desc. This keeps ingestion generic.

    region_map format supports:
      {
        "regions": {...},
        "region_keywords": {
          "us_gulf": ["gulf", "louisiana", "houston"],
          ...
        }
      }
    """
    if not area_desc:
        return []
    area = str(area_desc).lower()
    kw = (region_map or {}).get("region_keywords") or {}
    out: List[str] = []
    for rid, words in kw.items():
        if not isinstance(words, list):
            continue
        for w in words:
            try:
                if str(w).lower() in area:
                    out.append(str(rid))
                    break
            except Exception:
                continue
    # de-dupe
    seen = set()
    uniq = []
    for r in out:
        if r in seen:
            continue
        seen.add(r)
        uniq.append(r)
    return uniq


def _load_region_map(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _ingest_once() -> None:
    ts0 = _utc_ms()
    region_map_path = os.environ.get("WEATHER_REGION_MAP", os.path.join("data", "weather_region_map.json"))
    region_map = _load_region_map(region_map_path)

    ok = 1
    err = None
    n = 0

    con = connect()
    try:
        data = _http_json("https://api.weather.gov/alerts/active")
        if not data:
            ok = 0
            err = "no_data"
        else:
            feats = data.get("features") or []
            for f in feats:
                try:
                    props = (f or {}).get("properties") or {}
                    geom = (f or {}).get("geometry")

                    alert_id = props.get("id") or props.get("@id") or props.get("identifier") or ""
                    if not alert_id:
                        continue

                    area_desc = props.get("areaDesc") or ""
                    # If user configured NWS_AREAS, filter by codes (best-effort string match)
                    if NWS_AREAS:
                        # NWS alerts often include state abbreviations in areaDesc or in geocode
                        area_u = str(area_desc).upper()
                        if not any(code in area_u for code in NWS_AREAS):
                            continue

                    issued_ts = _iso_to_ms(props.get("sent") or props.get("effective") or props.get("onset"))
                    effective_ts = _iso_to_ms(props.get("effective") or props.get("onset"))
                    expires_ts = _iso_to_ms(props.get("expires"))

                    event = props.get("event") or ""
                    severity = props.get("severity") or ""
                    urgency = props.get("urgency") or ""
                    certainty = props.get("certainty") or ""

                    headline = props.get("headline") or ""
                    description = props.get("description") or ""

                    affected_regions = _normalize_regions_from_area_desc(area_desc, region_map)
                    aff_json = json.dumps(affected_regions, separators=(",", ":"), sort_keys=True)

                    con.execute(
                        """
                        INSERT OR IGNORE INTO weather_alerts(
                          provider, alert_id,
                          issued_ts, effective_ts, expires_ts,
                          event, severity, urgency, certainty,
                          area_desc, polygon_geojson, affected_regions,
                          headline, description, source_uri
                        )
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            PROVIDER,
                            str(alert_id),
                            int(issued_ts or ts0),
                            int(effective_ts or 0),
                            int(expires_ts or 0),
                            str(event),
                            str(severity),
                            str(urgency),
                            str(certainty),
                            str(area_desc),
                            _poly_to_geojson(geom),
                            aff_json,
                            str(headline),
                            str(description),
                            "https://api.weather.gov/alerts/active",
                        ),
                    )
                    n += 1
                except Exception as e:
                    ok = 0
                    err = repr(e)

        # provider health (reuse weather_provider_health table)
        con.execute(
            """
            INSERT INTO weather_provider_health(ts_ms, provider, ok, latency_ms, error)
            VALUES (?,?,?,?,?)
            ON CONFLICT(provider, ts_ms) DO UPDATE SET
              ok=excluded.ok,
              latency_ms=excluded.latency_ms,
              error=excluded.error
            """,
            (int(ts0), str(PROVIDER), int(ok), None, (str(err) if err else None)),
        )
        con.commit()

        LOG.info("weather_alerts_ingest inserted=%d ok=%s", n, ok)
    finally:
        try:
            con.close()
        except Exception:
            pass


def main():
    init_db()
    if not acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=LOCK_STALE_AFTER_S):
        raise SystemExit(2)

    last_hb = 0.0
    try:
        while True:
            now = time.time()
            if now - last_hb > 30:
                touch_job_lock(JOB_NAME, OWNER, PID)
                put_job_heartbeat(
                    JOB_NAME,
                    OWNER,
                    PID,
                    extra_json=json.dumps({"provider": PROVIDER}, separators=(",", ":")),
                )
                last_hb = now

            _ingest_once()
            time.sleep(float(POLL_INTERVAL_S))
    finally:
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
