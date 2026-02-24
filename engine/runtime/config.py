# engine/runtime/config.py
"""
Runtime-owned environment configuration.

Purpose:
- Single source of truth for runtime knobs (supervisor/job manager/orchestrator/dashboard).
- Environment-driven only (Docker/Postgres migration ready later).
- No behavior changes: values match existing defaults in dashboard_server.py.
"""

from __future__ import annotations

import os

def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

# -----------------------------
# Auto-restart guards
# -----------------------------
AUTO_RESTART_DAEMONS = os.environ.get("AUTO_RESTART_DAEMONS", "1") == "1"
DAEMON_RESTART_BASE_DELAY_MS = int(os.environ.get("DAEMON_RESTART_BASE_DELAY_MS", "2000"))
DAEMON_RESTART_MAX_DELAY_MS = int(os.environ.get("DAEMON_RESTART_MAX_DELAY_MS", "30000"))
DAEMON_RESTART_WINDOW_S = int(os.environ.get("DAEMON_RESTART_WINDOW_S", "120"))
DAEMON_RESTART_MAX_IN_WINDOW = int(os.environ.get("DAEMON_RESTART_MAX_IN_WINDOW", "5"))
DAEMON_WATCHDOG_PERIOD_S = float(os.environ.get("DAEMON_WATCHDOG_PERIOD_S", "1.0"))

# -----------------------------
# Auto pipeline / challenger / size policy (dashboard scheduler knobs)
# -----------------------------
AUTO_RECALIBRATE = os.environ.get("AUTO_RECALIBRATE", "1") == "1"
AUTO_RECALIBRATE_INTERVAL_S = 86400  # daily (fixed default as in dashboard_server.py)

AUTO_SIZE_POLICY = os.environ.get("AUTO_SIZE_POLICY", "0") == "1"
AUTO_SIZE_POLICY_INTERVAL_S = float(os.environ.get("AUTO_SIZE_POLICY_INTERVAL_S", "86400"))  # daily
AUTO_SIZE_POLICY_START_DELAY_S = float(os.environ.get("AUTO_SIZE_POLICY_START_DELAY_S", "20.0"))
AUTO_SIZE_POLICY_LOG = os.environ.get("AUTO_SIZE_POLICY_LOG", "1") == "1"

AUTO_PIPELINE = os.environ.get("AUTO_PIPELINE", "0") == "1"
AUTO_PIPELINE_INTERVAL_S = float(os.environ.get("AUTO_PIPELINE_INTERVAL_S", "300"))  # 5 min
AUTO_PIPELINE_START_DELAY_S = float(os.environ.get("AUTO_PIPELINE_START_DELAY_S", "2.0"))
AUTO_PIPELINE_LOG = os.environ.get("AUTO_PIPELINE_LOG", "1") == "1"

AUTO_CHALLENGER = os.environ.get("AUTO_CHALLENGER", "0") == "1"
AUTO_CHALLENGER_INTERVAL_S = float(os.environ.get("AUTO_CHALLENGER_INTERVAL_S", "3600"))  # 1h
AUTO_CHALLENGER_START_DELAY_S = float(os.environ.get("AUTO_CHALLENGER_START_DELAY_S", "10.0"))
AUTO_CHALLENGER_LOG = os.environ.get("AUTO_CHALLENGER_LOG", "1") == "1"

AUTO_CHALLENGER_MIN_DRIFT = float(os.environ.get("AUTO_CHALLENGER_MIN_DRIFT", "0.0"))  # 0 disables gate

AUTO_PIPELINE_INCLUDE_EXECUTION = os.environ.get("AUTO_PIPELINE_INCLUDE_EXECUTION", "0") == "1"

# -----------------------------
# Health thresholds (used by health + dashboard explanations)
# -----------------------------
HEALTH_PRICES_MAX_AGE_S = float(os.environ.get("HEALTH_PRICES_MAX_AGE_S", "120"))
HEALTH_EVENTS_MAX_AGE_S = float(os.environ.get("HEALTH_EVENTS_MAX_AGE_S", "600"))
HEALTH_PREDICTIONS_MAX_AGE_S = float(os.environ.get("HEALTH_PREDICTIONS_MAX_AGE_S", "600"))
HEALTH_JOBS_MAX_STALE_S = float(os.environ.get("HEALTH_JOBS_MAX_STALE_S", "180"))

HEALTH_MIN_LABELS = int(os.environ.get("HEALTH_MIN_LABELS", "10"))
HEALTH_MIN_MODEL_SUPPORT = int(os.environ.get("HEALTH_MIN_MODEL_SUPPORT", "10"))

# -----------------------------
# Training auto-resume policy
# -----------------------------
TRAINING_RESUME_MIN_OK_STREAK = int(os.environ.get("TRAINING_RESUME_MIN_OK_STREAK", "5"))

# -----------------------------
# Preflight
# -----------------------------
PREFLIGHT_ENABLE = os.environ.get("PREFLIGHT_ENABLE", "1") == "1"
PREFLIGHT_BLOCK_JOBS = os.environ.get("PREFLIGHT_BLOCK_JOBS", "1") == "1"
PREFLIGHT_PRICES_MAX_AGE_S = float(os.environ.get("PREFLIGHT_PRICES_MAX_AGE_S", "300"))
