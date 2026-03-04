import json
import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Any


@dataclass(frozen=True)
class HorizonSpec:
    name: str
    horizon_s: int
    max_expected_hold_s: int
    label_price_lag_ms: int
    exec_entry_lag_ms: int
    exec_exit_lag_ms: int
    cost_fees_bps: float
    cost_slippage_bps: float


_DEFAULT_SPECS: List[HorizonSpec] = [
    HorizonSpec(
        name="5m",
        horizon_s=5 * 60,
        max_expected_hold_s=30 * 60,
        label_price_lag_ms=0,
        exec_entry_lag_ms=250,
        exec_exit_lag_ms=250,
        cost_fees_bps=0.5,
        cost_slippage_bps=2.5,
    ),
    HorizonSpec(
        name="1h",
        horizon_s=60 * 60,
        max_expected_hold_s=6 * 60 * 60,
        label_price_lag_ms=0,
        exec_entry_lag_ms=750,
        exec_exit_lag_ms=750,
        cost_fees_bps=0.5,
        cost_slippage_bps=2.0,
    ),
    HorizonSpec(
        name="1d",
        horizon_s=24 * 60 * 60,
        max_expected_hold_s=3 * 24 * 60 * 60,
        label_price_lag_ms=0,
        exec_entry_lag_ms=1500,
        exec_exit_lag_ms=1500,
        cost_fees_bps=0.5,
        cost_slippage_bps=1.5,
    ),
    HorizonSpec(
        name="3d",
        horizon_s=3 * 24 * 60 * 60,
        max_expected_hold_s=7 * 24 * 60 * 60,
        label_price_lag_ms=0,
        exec_entry_lag_ms=2500,
        exec_exit_lag_ms=2500,
        cost_fees_bps=0.5,
        cost_slippage_bps=1.25,
    ),
    HorizonSpec(
        name="2w",
        horizon_s=14 * 24 * 60 * 60,
        max_expected_hold_s=21 * 24 * 60 * 60,
        label_price_lag_ms=0,
        exec_entry_lag_ms=4000,
        exec_exit_lag_ms=4000,
        cost_fees_bps=0.5,
        cost_slippage_bps=1.0,
    ),
]


def _parse_json_env(name: str) -> Optional[Any]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def list_horizon_specs() -> List[HorizonSpec]:
    override = _parse_json_env("HORIZON_SPECS_JSON")
    if isinstance(override, list) and override:
        out: List[HorizonSpec] = []
        for row in override:
            if not isinstance(row, dict):
                continue
            try:
                out.append(
                    HorizonSpec(
                        name=str(row.get("name")),
                        horizon_s=int(row.get("horizon_s")),
                        max_expected_hold_s=int(row.get("max_expected_hold_s")),
                        label_price_lag_ms=int(row.get("label_price_lag_ms", 0)),
                        exec_entry_lag_ms=int(row.get("exec_entry_lag_ms", 0)),
                        exec_exit_lag_ms=int(row.get("exec_exit_lag_ms", 0)),
                        cost_fees_bps=float(row.get("cost_fees_bps", 0.5)),
                        cost_slippage_bps=float(row.get("cost_slippage_bps", 2.0)),
                    )
                )
            except Exception:
                continue
        if out:
            return out
    return list(_DEFAULT_SPECS)


def horizons_s() -> List[int]:
    return [int(s.horizon_s) for s in list_horizon_specs()]


def horizons_map() -> Dict[str, int]:
    return {str(s.name): int(s.horizon_s) for s in list_horizon_specs()}


def get_spec(horizon_s: int) -> Optional[HorizonSpec]:
    h = int(horizon_s)
    for s in list_horizon_specs():
        if int(s.horizon_s) == h:
            return s
    return None


def horizon_priority(horizon_s: int) -> float:
    """Priority multiplier used for capital allocation across horizons."""
    try:
        h = int(horizon_s)
    except Exception:
        return 1.0

    raw = _parse_json_env("HORIZON_PRIORITY_JSON")
    if isinstance(raw, dict) and raw:
        try:
            v = raw.get(str(h))
            if v is None:
                v = raw.get(str(get_spec(h).name)) if get_spec(h) else None
            if v is not None:
                vv = float(v)
                return vv if vv == vv and vv > 0 else 1.0
        except Exception:
            pass

    s = get_spec(h)
    if not s:
        return 1.0

    if s.name == "5m":
        return 1.15
    if s.name == "1h":
        return 1.05
    if s.name == "1d":
        return 1.0
    if s.name == "3d":
        return 0.95
    if s.name == "2w":
        return 0.90
    return 1.0
