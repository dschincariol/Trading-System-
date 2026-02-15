# FILE: engine/api/server.py
# FIND (replace entire file content)
"""
HTTP Server Layer

Owns:
- HTTPServer wiring
- Handler
- Route registration
- run_server()

Does NOT own:
- Job logic
- Orchestration logic
- Health logic
"""

import os
import sys
import threading
import time

from engine.api.http_transport import build_handler, run_http_server

from engine.runtime.supervisor import RuntimeSupervisor
from engine.runtime.jobs_manager import JobManager
from engine.runtime.orchestrator import RuntimeOrchestrator
from engine.runtime.locks import (
    acquire_lock,
    release_lock,
)
from engine.runtime.job_registry import ALLOWED_JOBS

from engine.runtime.lifecycle import (
    start_lifecycle_monitor,
    mark_shutdown,
)

# Route specs (metadata only)
from engine.api.api_system import ROUTE_SPECS_SYSTEM
from engine.api.api_jobs import ROUTE_SPECS_JOBS
from engine.api.api_ops import ROUTE_SPECS_OPS


# ---------------------------------------------------
# Load .env if present (safe no-op if missing)
# ---------------------------------------------------
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


# ---------------------------------------------------
# Ensure static UI paths resolve (serve /ui/* correctly)
# ---------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_BASE_DIR, "..", ".."))
try:
    os.chdir(_PROJECT_ROOT)
except Exception:
    pass


# ---------------------------------------------------
# ENV CONFIG
# ---------------------------------------------------
HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = int(os.environ.get("DASHBOARD_PORT", "8000"))
DASHBOARD_API_TOKEN = os.environ.get("DASHBOARD_API_TOKEN", "").strip()

AUTO_BOOT_DAEMONS = os.environ.get("AUTO_BOOT_DAEMONS", "0") == "1"
AUTO_BOOT_TARGETS = [
    x.strip()
    for x in os.environ.get("AUTO_BOOT_TARGETS", "").split(",")
    if x.strip()
]


# ---------------------------------------------------
# RUNTIME WIRES
# ---------------------------------------------------
JOBS = JobManager()

SUPERVISOR = RuntimeSupervisor(jobs=JOBS)

ORCHESTRATOR = RuntimeOrchestrator(
    jobs=JOBS,
    acquire_lock=acquire_lock,
    release_lock=release_lock,
    auto_pipeline_include_execution=os.environ.get("AUTO_PIPELINE_INCLUDE_EXECUTION", "0") == "1",
    auto_pipeline_log=os.environ.get("AUTO_PIPELINE_LOG", "0") == "1",
    auto_pipeline_interval_s=float(os.environ.get("AUTO_PIPELINE_INTERVAL_S", "300")),
    auto_pipeline_start_delay_s=float(os.environ.get("AUTO_PIPELINE_START_DELAY_S", "5")),
    auto_challenger_log=os.environ.get("AUTO_CHALLENGER_LOG", "0") == "1",
    auto_challenger_interval_s=float(os.environ.get("AUTO_CHALLENGER_INTERVAL_S", "900")),
    auto_challenger_start_delay_s=float(os.environ.get("AUTO_CHALLENGER_START_DELAY_S", "10")),
    auto_challenger_min_drift=float(os.environ.get("AUTO_CHALLENGER_MIN_DRIFT", "0.0")),
    auto_size_policy_log=os.environ.get("AUTO_SIZE_POLICY_LOG", "0") == "1",
    auto_size_policy_interval_s=float(os.environ.get("AUTO_SIZE_POLICY_INTERVAL_S", "3600")),
    auto_size_policy_start_delay_s=float(os.environ.get("AUTO_SIZE_POLICY_START_DELAY_S", "15")),
)


# ---------------------------------------------------
# API HANDLERS (single import point)
# ---------------------------------------------------
from engine.api.api_handlers import (  # noqa: E402
    api_get_health,
    api_get_kill_switches,
    api_get_system_state,
    api_get_jobs,
    api_get_job_log,
    api_get_job_history,
    api_post_job_start,
    api_post_job_stop,
    api_post_pipeline_run,
    api_get_alerts,
    api_get_validation,
    api_get_model_diagnostics,
    api_get_model_registry,
    api_get_embed_model_eval,
    api_get_embed_conf_calib,
    api_get_temporal_eval,
    api_get_temporal_models,
    api_get_latest_portfolio_backtest,
    api_get_execution_metrics,
    api_get_execution_metrics_rolling,
    api_get_execution_metrics_by_symbol,
    api_get_execution_cost_by_confidence,
    api_get_social_features,
    api_get_social_regimes,
    api_get_social_blocks,
    api_get_confidence_mass,
    api_post_rollback,
)

API_HANDLERS = {
    # system
    "api_get_health": api_get_health,
    "api_get_kill_switches": api_get_kill_switches,
    "api_get_system_state": api_get_system_state,
    # jobs
    "api_get_jobs": api_get_jobs,
    "api_get_job_log": api_get_job_log,
    "api_get_job_history": api_get_job_history,
    "api_post_job_start": api_post_job_start,
    "api_post_job_stop": api_post_job_stop,
    "api_post_pipeline_run": api_post_pipeline_run,
    # ops
    "api_get_alerts": api_get_alerts,
    "api_get_validation": api_get_validation,
    "api_get_model_diagnostics": api_get_model_diagnostics,
    "api_get_model_registry": api_get_model_registry,
    "api_get_embed_model_eval": api_get_embed_model_eval,
    "api_get_embed_conf_calib": api_get_embed_conf_calib,
    "api_get_temporal_eval": api_get_temporal_eval,
    "api_get_temporal_models": api_get_temporal_models,
    "api_get_latest_portfolio_backtest": api_get_latest_portfolio_backtest,
    "api_get_execution_metrics": api_get_execution_metrics,
    "api_get_execution_metrics_rolling": api_get_execution_metrics_rolling,
    "api_get_execution_metrics_by_symbol": api_get_execution_metrics_by_symbol,
    "api_get_execution_cost_by_confidence": api_get_execution_cost_by_confidence,
    "api_get_social_features": api_get_social_features,
    "api_get_social_regimes": api_get_social_regimes,
    "api_get_social_blocks": api_get_social_blocks,
    "api_get_confidence_mass": api_get_confidence_mass,
    "api_post_rollback": api_post_rollback,
}


# ---------------------------------------------------
# ROUTE TABLE
# ---------------------------------------------------
ROUTE_SPECS = []
ROUTE_SPECS.extend(ROUTE_SPECS_SYSTEM or [])
ROUTE_SPECS.extend(ROUTE_SPECS_JOBS or [])
ROUTE_SPECS.extend(ROUTE_SPECS_OPS or [])


def _route_coverage_check(route_specs, api_handlers):
    expected = set()
    for m, p, h in route_specs or []:
        expected.add(str(h))
    provided = set(api_handlers.keys())

    missing = sorted([h for h in expected if h not in provided])
    if missing:
        raise RuntimeError(f"missing API handlers for ROUTE_SPECS: {missing}")


# ---------------------------------------------------
# SERVER
# ---------------------------------------------------
_HTTPD = None


def run_server():
    global _HTTPD

    # route sanity (fail fast)
    _route_coverage_check(ROUTE_SPECS, API_HANDLERS)

    # lifecycle monitor (safe best-effort)
    try:
        start_lifecycle_monitor(
            get_health=lambda: api_get_health(None, {}),
            get_jobs=lambda: JOBS.list_jobs(),
            get_kill_switches=lambda: api_get_kill_switches(None, {"JOBS": JOBS}),
            interval_s=2.0,
        )
    except Exception:
        pass

    # deterministic auto-boot (optional)
    if AUTO_BOOT_DAEMONS and AUTO_BOOT_TARGETS:
        try:
            SUPERVISOR.deterministic_start(
                AUTO_BOOT_TARGETS,
                include_deps=True,
                strict=False,
            )
        except Exception:
            pass

    HandlerCls = build_handler(
        ROUTE_SPECS=ROUTE_SPECS,
        API_HANDLERS=API_HANDLERS,
        dashboard_api_token=DASHBOARD_API_TOKEN,
        ctx={
            "JOBS": JOBS,
            "SUPERVISOR": SUPERVISOR,
            "ORCHESTRATOR": ORCHESTRATOR,
            "ALLOWED_JOBS": ALLOWED_JOBS,
        },
    )

    _HTTPD = run_http_server(HOST, PORT, HandlerCls)
    print(f"Dashboard running at http://{HOST}:{PORT}/ui/dashboard.html")

    # graceful shutdown signals
    try:
        import signal

        def _shutdown(_sig=None, _frame=None):
            try:
                mark_shutdown()
            except Exception:
                pass
            try:
                if _HTTPD:
                    _HTTPD.shutdown()
            except Exception:
                pass

        try:
            signal.signal(signal.SIGINT, _shutdown)
        except Exception:
            pass
        try:
            signal.signal(signal.SIGTERM, _shutdown)
        except Exception:
            pass
    except Exception:
        pass

    try:
        _HTTPD.serve_forever()
    finally:
        try:
            mark_shutdown()
        except Exception:
            pass
        try:
            JOBS.stop_all()
        except Exception:
            pass
        try:
            if _HTTPD:
                _HTTPD.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    run_server()
