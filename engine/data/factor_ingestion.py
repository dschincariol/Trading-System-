# dev_core/factor_ingestion.py
"""
Leakage-safe external factor ingestion utilities.

This module standardizes how external factors enter the system:
- factor_registry: metadata about factors
- factor_observations: raw observed values with (asof_ts, effective_ts, version)
- factor_features: derived/transformed features for model consumption

Key concepts:
- asof_ts: the timestamp when you learned/ingested the data (decision-time safe)
- effective_ts: the timestamp the value applies to (event time / target time)
- version: allows revisions (macro revisions, forecast model reruns, restatements)
"""

import json
import math
import time
from typing import Any, Dict, Optional, Tuple, List

from engine.storage import connect


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_float(x) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def ensure_factor_registry(
    con,
    *,
    factor_id: str,
    family: str,
    name: str,
    cadence: str,
    release_lag_sec: int = 0,
    applies_to: Optional[str] = None,
    units: Optional[str] = None,
    transform: Optional[str] = None,
    is_revisioned: bool = False,
    source: Optional[str] = None,
    enabled: bool = True,
) -> None:
    """
    Idempotent upsert into factor_registry.
    """
    con.execute(
        """
        INSERT OR REPLACE INTO factor_registry(
          factor_id, family, name, cadence, release_lag_sec,
          applies_to, units, transform, is_revisioned, source, enabled
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            str(factor_id),
            str(family),
            str(name),
            str(cadence),
            int(release_lag_sec or 0),
            (str(applies_to) if applies_to else None),
            (str(units) if units else None),
            (str(transform) if transform else None),
            (1 if bool(is_revisioned) else 0),
            (str(source) if source else None),
            (1 if bool(enabled) else 0),
        ),
    )


def put_factor_observation(
    con,
    *,
    factor_id: str,
    asof_ts: int,
    effective_ts: int,
    value: Optional[float],
    version: int = 1,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Inserts a raw observation into factor_observations (revision-safe).
    """
    con.execute(
        """
        INSERT OR REPLACE INTO factor_observations(
          factor_id, asof_ts, effective_ts, value, version, meta_json
        )
        VALUES (?,?,?,?,?,?)
        """,
        (
            str(factor_id),
            int(asof_ts),
            int(effective_ts),
            _safe_float(value),
            int(version or 1),
            json.dumps(meta or {}, separators=(",", ":"), sort_keys=True),
        ),
    )


def get_factor_value_asof(
    con,
    *,
    factor_id: str,
    ts_ms: int,
) -> Optional[float]:
    """
    Leakage-safe as-of lookup:
    - eligible rows have asof_ts <= ts_ms AND effective_ts <= ts_ms
    - picks latest by asof_ts, then effective_ts, then version
    """
    row = con.execute(
        """
        SELECT value
        FROM factor_observations
        WHERE factor_id=?
          AND asof_ts <= ?
          AND effective_ts <= ?
        ORDER BY asof_ts DESC, effective_ts DESC, version DESC
        LIMIT 1
        """,
        (str(factor_id), int(ts_ms), int(ts_ms)),
    ).fetchone()
    if not row:
        return None
    return _safe_float(row[0])


def put_factor_feature(
    con,
    *,
    feature_id: str,
    asof_ts: int,
    effective_ts: int,
    value: Optional[float],
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Inserts a derived feature value into factor_features.
    """
    con.execute(
        """
        INSERT OR REPLACE INTO factor_features(
          feature_id, asof_ts, effective_ts, value, meta_json
        )
        VALUES (?,?,?,?,?)
        """,
        (
            str(feature_id),
            int(asof_ts),
            int(effective_ts),
            _safe_float(value) or 0.0,
            json.dumps(meta or {}, separators=(",", ":"), sort_keys=True),
        ),
    )


def materialize_simple_feature(
    con,
    *,
    factor_id: str,
    feature_id: str,
    ts_ms: int,
    default: float = 0.0,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Convenience: read factor value as-of ts_ms and write to factor_features.
    """
    v = get_factor_value_asof(con, factor_id=str(factor_id), ts_ms=int(ts_ms))
    put_factor_feature(
        con,
        feature_id=str(feature_id),
        asof_ts=int(ts_ms),
        effective_ts=int(ts_ms),
        value=(v if v is not None else float(default)),
        meta=meta or {"src_factor_id": str(factor_id)},
    )


def ingest_batch_observations(
    *,
    factor_id: str,
    rows: List[Tuple[int, int, Optional[float], int, Optional[Dict[str, Any]]]],
    registry: Optional[Dict[str, Any]] = None,
) -> int:
    """
    High-throughput helper:
    rows = [(asof_ts, effective_ts, value, version, meta_dict), ...]

    If registry is provided, it is used to ensure factor_registry is populated.
    Returns number of inserted rows (best-effort).
    """
    con = connect()
    inserted = 0
    try:
        if registry:
            ensure_factor_registry(con, factor_id=str(factor_id), **registry)

        for asof_ts, effective_ts, value, version, meta in (rows or []):
            put_factor_observation(
                con,
                factor_id=str(factor_id),
                asof_ts=int(asof_ts),
                effective_ts=int(effective_ts),
                value=value,
                version=int(version or 1),
                meta=meta,
            )
            inserted += 1

        con.commit()
        return int(inserted)
    finally:
        try:
            con.close()
        except Exception:
            pass


def example_register_and_ingest_skeleton() -> None:
    """
    Non-executing example function you can call manually.
    Keeps module self-contained for future ingest jobs.
    """
    ts = _now_ms()
    con = connect()
    try:
        ensure_factor_registry(
            con,
            factor_id="macro.cpi_yoy",
            family="macro",
            name="US CPI YoY",
            cadence="monthly",
            release_lag_sec=3600,
            units="pct",
            transform="zscore_24m",
            is_revisioned=True,
            source="bls",
            enabled=True,
        )
        put_factor_observation(
            con,
            factor_id="macro.cpi_yoy",
            asof_ts=ts,
            effective_ts=ts,
            value=3.1,
            version=1,
            meta={"note": "example"},
        )
        con.commit()
    finally:
        try:
            con.close()
        except Exception:
            pass
