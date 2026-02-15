# engine/runtime/config.py
"""
Runtime-owned environment configuration.

Purpose:
- Remove runtime coupling to dashboard_config.py
- Keep supervisor/job manager deterministic + environment-driven
"""

from __future__ import annotations

import os

# Auto-restart guards
AUTO_RESTART_DAEMONS = os.environ.get("AUTO_RESTART_DAEMONS", "1") == "1"
DAEMON_RESTART_BASE_DELAY_MS = int(os.environ.get("DAEMON_RESTART_BASE_DELAY_MS", "2000"))
DAEMON_RESTART_MAX_DELAY_MS = int(os.environ.get("DAEMON_RESTART_MAX_DELAY_MS", "30000"))
DAEMON_RESTART_WINDOW_S = int(os.environ.get("DAEMON_RESTART_WINDOW_S", "120"))
DAEMON_RESTART_MAX_IN_WINDOW = int(os.environ.get("DAEMON_RESTART_MAX_IN_WINDOW", "5"))
DAEMON_WATCHDOG_PERIOD_S = float(os.environ.get("DAEMON_WATCHDOG_PERIOD_S", "1.0"))

# Preflight
PREFLIGHT_ENABLE = os.environ.get("PREFLIGHT_ENABLE", "1") == "1"
PREFLIGHT_BLOCK_JOBS = os.environ.get("PREFLIGHT_BLOCK_JOBS", "1") == "1"
