# engine/runtime/config_schema.py
import os
from dataclasses import dataclass
from typing import Optional


class ConfigError(RuntimeError):
    pass


def _req(name: str) -> str:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        raise ConfigError(f"Missing required env: {name}")
    return str(v).strip()


def _opt(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None else str(v).strip()


def _opt_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return int(default)
    try:
        return int(str(v).strip())
    except Exception as e:
        raise ConfigError(f"Invalid int for {name}: {v}") from e


def _opt_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return float(default)
    try:
        return float(str(v).strip())
    except Exception as e:
        raise ConfigError(f"Invalid float for {name}: {v}") from e


def _opt_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    s = str(v).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class RuntimeConfig:
    env: str
    db_path: str

    # production lock
    prod_lock: bool
    allow_training: bool

    # supervisor
    supervisor_enabled: bool
    supervisor_tick_s: int

    # execution barrier thresholds
    exec_degrade_block: bool
    exec_degrade_warn_cost_pct: float
    exec_degrade_crit_cost_pct: float


def load_runtime_config() -> RuntimeConfig:
    env = _opt("ENV", "dev")
    db_path = _req("DB_PATH")

    prod_lock = _opt_bool("PROD_LOCK", default=(env == "prod"))
    allow_training = _opt_bool("ALLOW_TRAINING", default=(env != "prod"))

    supervisor_enabled = _opt_bool("SUPERVISOR_ENABLED", default=True)
    supervisor_tick_s = _opt_int("SUPERVISOR_TICK_S", 2)

    exec_degrade_block = _opt_bool("EXEC_DEGRADE_BLOCK", default=True)
    exec_degrade_warn_cost_pct = _opt_float("EXEC_DEGRADE_WARN_COST_PCT", 0.25)
    exec_degrade_crit_cost_pct = _opt_float("EXEC_DEGRADE_CRIT_COST_PCT", 0.50)

    # Hard production safety: do not allow training when prod_lock enabled.
    if prod_lock and allow_training:
        raise ConfigError("PROD_LOCK=1 forbids ALLOW_TRAINING=1")

    return RuntimeConfig(
        env=env,
        db_path=db_path,
        prod_lock=prod_lock,
        allow_training=allow_training,
        supervisor_enabled=supervisor_enabled,
        supervisor_tick_s=supervisor_tick_s,
        exec_degrade_block=exec_degrade_block,
        exec_degrade_warn_cost_pct=exec_degrade_warn_cost_pct,
        exec_degrade_crit_cost_pct=exec_degrade_crit_cost_pct,
    )
