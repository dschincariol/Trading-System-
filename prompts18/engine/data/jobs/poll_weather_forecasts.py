# CREATE NEW FILE: poll_weather_forecasts.py
# (copy/paste entire file)

# poll_weather_forecasts.py
"""
Weather forecast poller (region-daily aggregates).

Default provider: Open-Meteo (no key).
Stores immutable as-issued rows in weather_forecast_region_daily.

Config:
- WEATHER_REGION_MAP=data/weather_region_map.json
- WEATHER_PROVIDER=open_meteo
- WEATHER_POLL_SECONDS=21600 (6h)

Region map format (minimum):
{
  "regions": {
    "us_pop": {"lat": 39.5, "lon": -98.35}
  },
  "symbols": {
    "SPY": ["us_pop"]
  }
}
"""

import os
import time
import json
import logging
from typing import Dict, Any, Tuple

import requests

from engine.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

_REGION_MAP_CACHE = None

# ------------------------------------------------------------
# Runtime config
# ------------------------------------------------------------

WEATHER_PROVIDER = os.environ.get("WEATHER_PROVIDER", "open_meteo").strip().lower()
WEATHER_REGION_MAP = os.environ.get("WEATHER_REGION_MAP", os.path.join("data", "weather_region_map.json"))

POLL_SECONDS = int(os.environ.get("WEATHER_POLL_SECONDS", "21600"))  # 6h default
JOB_NAME = "poll_weather_forecasts"
OWNER = os.environ.get("JOB_OWNER", os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")))
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "300"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "30.0"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [poll_weather_forecasts] %(message)s",
)

# Degree day base (F) -> C
DD_BASE_F = float(os.environ.get("WEATHER_DD_BASE_F", "65.0"))
DD_BASE_C = (DD_BASE_F - 32.0) * (5.0 / 9.0)


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


def _iso_date_to_day_ts_ms(s: str) -> int:
    # "YYYY-MM-DD" -> day start ms UTC
    try:
        y, m, d = s.split("-")
        import datetime as _dt
        dt = _dt.datetime(int(y), int(m), int(d), 0, 0, 0)
        return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
    except Exception:
        return 0


def _hdd_cdd_from_temp_c(tmean_c: float) -> Tuple[float, float]:
    # HDD/CDD relative to DD_BASE_C
    hdd = max(0.0, DD_BASE_C - float(tmean_c))
    cdd = max(0.0, float(tmean_c) - DD_BASE_C)
    return float(hdd), float(cdd)


def _fetch_open_meteo_daily(lat: float, lon: float) -> Dict[str, Any]:
    # Use daily aggregates to keep ingestion simple + stable.
    # Note: windspeed_10m_max is in km/h; we convert to m/s below.
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": float(lat),
        "longitude": float(lon),
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max",
        "timezone": "UTC",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json() or {}


def _upsert_region_day(con, provider: str, region_id: str, run_ts: int, day_ts: int,
                       temp_mean_c: float, hdd: float, cdd: float,
                       wind_mean_mps: float, precip_sum_mm: float, spread: float,
                       source_uri: str) -> None:
    con.execute(
        """
        INSERT OR IGNORE INTO weather_forecast_region_daily(
          provider, region_id, run_ts, day_ts,
          temp_mean_c, hdd, cdd, wind_mean_mps, precip_sum_mm, spread,
          source_uri
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(provider),
            str(region_id),
            int(run_ts),
            int(day_ts),
            float(temp_mean_c),
            float(hdd),
            float(cdd),
            float(wind_mean_mps),
            float(precip_sum_mm),
            float(spread),
            str(source_uri) if source_uri else None,
        ),
    )


def _run_once() -> None:
    cfg = _load_region_map()
    regions = (cfg or {}).get("regions") or {}
    if not isinstance(regions, dict) or not regions:
        logging.info("no regions configured in WEATHER_REGION_MAP; nothing to do")
        return

    now_ms = int(time.time() * 1000)
    put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"phase": "fetch", "provider": WEATHER_PROVIDER}))

    con = connect()
    try:
        for region_id, meta in regions.items():
            try:
                touch_job_lock(JOB_NAME, OWNER, PID)
                put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"phase": "region", "region": str(region_id)}))
            except Exception:
                pass
            try:
                lat = float(meta.get("lat"))
                lon = float(meta.get("lon"))
            except Exception:
                continue

            try:
                if WEATHER_PROVIDER != "open_meteo":
                    logging.info(f"provider={WEATHER_PROVIDER} not supported by this poller yet; skipping")
                    break

                payload = _fetch_open_meteo_daily(lat, lon)
                daily = (payload or {}).get("daily") or {}
                times = daily.get("time") or []
                tmax = daily.get("temperature_2m_max") or []
                tmin = daily.get("temperature_2m_min") or []
                pr = daily.get("precipitation_sum") or []
                wmax = daily.get("windspeed_10m_max") or []

                # Deterministic provenance (request URL)
                source_uri = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max&timezone=UTC"
                run_ts = now_ms  # snapshot time

                n = min(len(times), len(tmax), len(tmin), len(pr), len(wmax))
                for i in range(n):
                    day_ts = _iso_date_to_day_ts_ms(str(times[i]))
                    if day_ts <= 0:
                        continue

                    tm = (float(tmax[i]) + float(tmin[i])) / 2.0
                    hdd, cdd = _hdd_cdd_from_temp_c(tm)

                    # open-meteo windspeed_10m_max is km/h -> m/s
                    wind_mps = float(wmax[i]) / 3.6

                    _upsert_region_day(
                        con,
                        provider=WEATHER_PROVIDER,
                        region_id=str(region_id),
                        run_ts=int(run_ts),
                        day_ts=int(day_ts),
                        temp_mean_c=float(tm),
                        hdd=float(hdd),
                        cdd=float(cdd),
                        wind_mean_mps=float(wind_mps),
                        precip_sum_mm=float(pr[i]),
                        spread=float(meta.get("spread", 0.0) or 0.0),
                        source_uri=str(source_uri) if source_uri is not None else "",
                    )

                con.commit()
                logging.info(f"ingested daily forecast region={region_id} n_days={n}")

            except Exception as e:
                logging.warning(f"region={region_id} forecast fetch failed: {e}")

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
            time.sleep(max(10, int(POLL_SECONDS)))

    finally:
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
