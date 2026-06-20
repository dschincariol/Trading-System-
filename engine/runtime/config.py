# engine/runtime/config.py
"""
Runtime-owned environment configuration.

Purpose:
- Single source of truth for runtime knobs (supervisor/job manager/orchestrator/dashboard).
- Environment-driven only.
- Safe defaults that DO NOT deadlock warmup.
"""

from __future__ import annotations
import os


# ---------------------------------------------------------
# helpers
# ---------------------------------------------------------

def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except Exception:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except Exception:
        return default


# ---------------------------------------------------------
# Auto-restart guards (MUST be enabled by default)
# ---------------------------------------------------------

AUTO_RESTART_DAEMONS = _env_bool("AUTO_RESTART_DAEMONS", True)

DAEMON_RESTART_BASE_DELAY_MS = _env_int("DAEMON_RESTART_BASE_DELAY_MS", 2000)
DAEMON_RESTART_MAX_DELAY_MS = _env_int("DAEMON_RESTART_MAX_DELAY_MS", 30000)
DAEMON_RESTART_WINDOW_S = _env_int("DAEMON_RESTART_WINDOW_S", 120)
DAEMON_RESTART_MAX_IN_WINDOW = _env_int("DAEMON_RESTART_MAX_IN_WINDOW", 5)
DAEMON_WATCHDOG_PERIOD_S = _env_float("DAEMON_WATCHDOG_PERIOD_S", 1.0)


# ---------------------------------------------------------
# Scheduler knobs
# ---------------------------------------------------------

AUTO_RECALIBRATE = _env_bool("AUTO_RECALIBRATE", True)
AUTO_RECALIBRATE_INTERVAL_S = 86400

AUTO_SIZE_POLICY = _env_bool("AUTO_SIZE_POLICY", False)
AUTO_SIZE_POLICY_INTERVAL_S = _env_float("AUTO_SIZE_POLICY_INTERVAL_S", 86400.0)
AUTO_SIZE_POLICY_START_DELAY_S = _env_float("AUTO_SIZE_POLICY_START_DELAY_S", 20.0)
AUTO_SIZE_POLICY_LOG = _env_bool("AUTO_SIZE_POLICY_LOG", True)

AUTO_PIPELINE = _env_bool("AUTO_PIPELINE", False)
AUTO_PIPELINE_INTERVAL_S = _env_float("AUTO_PIPELINE_INTERVAL_S", 300.0)
AUTO_PIPELINE_START_DELAY_S = _env_float("AUTO_PIPELINE_START_DELAY_S", 2.0)
AUTO_PIPELINE_LOG = _env_bool("AUTO_PIPELINE_LOG", True)

AUTO_CHALLENGER = _env_bool("AUTO_CHALLENGER", False)
AUTO_CHALLENGER_INTERVAL_S = _env_float("AUTO_CHALLENGER_INTERVAL_S", 3600.0)
AUTO_CHALLENGER_START_DELAY_S = _env_float("AUTO_CHALLENGER_START_DELAY_S", 10.0)
AUTO_CHALLENGER_LOG = _env_bool("AUTO_CHALLENGER_LOG", True)

AUTO_CHALLENGER_MIN_DRIFT = _env_float("AUTO_CHALLENGER_MIN_DRIFT", 0.0)
AUTO_PIPELINE_INCLUDE_EXECUTION = _env_bool("AUTO_PIPELINE_INCLUDE_EXECUTION", False)

# In SAFE, execution must be disabled even if other loops are enabled
EXECUTION_DISABLED_IN_SAFE = _env_bool("EXECUTION_DISABLED_IN_SAFE", True)


# ---------------------------------------------------------
# Health thresholds
# ---------------------------------------------------------

HEALTH_PRICES_MAX_AGE_S = _env_float("HEALTH_PRICES_MAX_AGE_S", 120.0)
HEALTH_EVENTS_MAX_AGE_S = _env_float("HEALTH_EVENTS_MAX_AGE_S", 600.0)
HEALTH_PREDICTIONS_MAX_AGE_S = _env_float("HEALTH_PREDICTIONS_MAX_AGE_S", 600.0)
HEALTH_JOBS_MAX_STALE_S = _env_float("HEALTH_JOBS_MAX_STALE_S", 180.0)

HEALTH_MIN_LABELS = _env_int("HEALTH_MIN_LABELS", 10)
HEALTH_MIN_MODEL_SUPPORT = _env_int("HEALTH_MIN_MODEL_SUPPORT", 10)


# ---------------------------------------------------------
# Training
# ---------------------------------------------------------

TRAINING_RESUME_MIN_OK_STREAK = _env_int("TRAINING_RESUME_MIN_OK_STREAK", 5)


# ---------------------------------------------------------
# Preflight
# ---------------------------------------------------------

PREFLIGHT_ENABLE = _env_bool("PREFLIGHT_ENABLE", True)

# CRITICAL:
# Do NOT block daemons during warmup or you deadlock.
PREFLIGHT_BLOCK_JOBS = _env_bool("PREFLIGHT_BLOCK_JOBS", False)

PREFLIGHT_PRICES_MAX_AGE_S = _env_float("PREFLIGHT_PRICES_MAX_AGE_S", 300.0)