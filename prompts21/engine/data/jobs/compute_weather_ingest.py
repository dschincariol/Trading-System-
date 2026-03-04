# compute_weather_ingest.py
"""
Weather ingestion job.

Responsibilities:
- Fetch regional daily forecasts (as-issued)
- Store into weather_forecast_region_daily
- Store provider health
- NEVER recompute past runs
- Leakage-safe by construction
"""

import os
import time
import json
import logging
import urllib.request
import calendar
from typing import Dict, Any, List, Optional

from engine.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

LOG = logging.getLogger("compute_weather_ingest")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

JOB_NAME = "compute_weather_ingest"
OWNER = os.environ.get("JOB_OWNER", "system")
PID = os.getpid()

WEATHER_PROVIDER = os.environ.get("WEATHER_PROVIDER", "open_meteo").lower()
REGION_MAP_PATH = os.environ.get(
    "WEATHER_REGION_MAP", os.path.join("data", "weather_region_map.json")
)

POLL_INTERVAL_S = int(os.environ.get("WEATHER_INGEST_INTERVAL_S", "1800"))  # 30 min
LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "300"))


# ------            -- ------------------------------------------------------
# Helpers
# ------            -- ------------------------------------------------------

def _load_region_map() -> Dict[str, Any]:
    try:
        with open(REGION_MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _utc_ms() -> int:
    return int(time.time() * 1000)


def _http_json(url: str, timeout: int = 20) -> Optional[Dict[str, Any]]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "weather-ingest/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


# ------            -- ------------------------------------------------------
# Provider adapters
# ------            -- ------------------------------------------------------

def _fetch_open_meteo(region_cfg: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """
    region_cfg example:
      {
        "lat": 41.9,
        "lon": -87.6,
        "timezone": "UTC"
      }
    """
    lat = region_cfg.get("lat")
    lon = region_cfg.get("lon")
    if lat is None or lon is None:
        return None

    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_mean,precipitation_sum,wind_speed_10m_mean"
        "&timezone=UTC"
    )

    data = _http_json(url)
    if not data:
        return None

    daily = data.get("daily") or {}
    days = daily.get("time") or []

    out = []
    for i, day in enumerate(days):
        try:
            day_ts = int(
                calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000
            )
            temp_c = float(daily["temperature_2m_mean"][i])
            precip = float(daily["precipitation_sum"][i])
            wind = float(daily["wind_speed_10m_mean"][i])

            # Degree-day approximations (C-based)
            hdd65 = max(0.0, (18.333 - temp_c))
            cdd65 = max(0.0, (temp_c - 18.333))

            out.append({
                "day_ts": day_ts,
                "temp_mean_c": temp_c,
                "hdd65": hdd65,
                "cdd65": cdd65,
                "wind_mean_mps": wind,
                "precip_sum_mm": precip,
                "spread": 0.0,  # ensemble spread unavailable here
            })
        except Exception:
            continue

    return out


# ------            -- ------------------------------------------------------
# Main ingest logic
# ------            -- ------------------------------------------------------

def _ingest_once() -> None:
    region_map = _load_region_map()
    regions = (region_map or {}).get("regions") or {}

    run_ts = _utc_ms()

    con = connect()
    try:
        ok = 1
        err = None
        n_rows = 0
        had_error = False

        for region_id, cfg in regions.items():
            try:
                if WEATHER_PROVIDER == "open_meteo":
                    days = _fetch_open_meteo(cfg)
                else:
                    continue

                if not days:
                    continue

                for d in days:
                    con.execute(
                        """
                        INSERT OR IGNORE INTO weather_forecast_region_daily(
                          provider, region_id, run_ts, day_ts,
                          temp_mean_c, hdd65, cdd65,
                          wind_mean_mps, precip_sum_mm, spread, source_uri
                        )
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            WEATHER_PROVIDER,
                            str(region_id),
                            int(run_ts),
                            int(d["day_ts"]),
                            float(d["temp_mean_c"]),
                            float(d["hdd65"]),
                            float(d["cdd65"]),
                            float(d["wind_mean_mps"]),
                            float(d["precip_sum_mm"]),
                            float(d["spread"]),
                            "open-meteo",
                        ),
                    )
                    n_rows += 1

            except Exception as e:
                had_error = True
                err = repr(e)

        

        con.execute(
            """
            INSERT INTO weather_provider_health
              (ts_ms, provider, ok, latency_ms, error)
            VALUES (?,?,?,?,?)
            ON CONFLICT(provider, ts_ms) DO UPDATE SET
              ok=excluded.ok,
              latency_ms=excluded.latency_ms,
              error=excluded.error
            """,
            (run_ts, WEATHER_PROVIDER, ok, None, err),
        )

        con.commit()
        LOG.info("weather_ingest rows=%d ok=%s", n_rows, ok)

    finally:
        con.close()


# ------            -- ------------------------------------------------------
# Job loop
# ------            -- ------------------------------------------------------

def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
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
                    extra_json=json.dumps({"provider": WEATHER_PROVIDER}),
                )
                last_hb = now

            _ingest_once()
            time.sleep(float(POLL_INTERVAL_S))

    finally:
        release_job_lock(JOB_NAME, OWNER, PID)

if __name__ == "__main__":
    main()
