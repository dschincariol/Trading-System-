# engine/api/server.py
"""
HTTP Server Layer

Extracted from dashboard_server.py

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
import json
import threading
from http.server import HTTPServer

from engine.runtime.supervisor import RuntimeSupervisor
from engine.runtime.jobs_manager import JobManager
from engine.runtime.orchestrator import RuntimeOrchestrator
from engine.runtime.health import (
    get_health_snapshot,
    run_preflight,
    preflight_cached,
    get_schema_audit,
)
from engine.runtime.locks import (
    acquire_lock,
    release_lock,
    write_job_history,
    read_job_history,
)
from engine.runtime.guards import (
    auto_rollback_loop,
)

from engine.runtime.job_registry import (
    ALLOWED_JOBS,
    PIPELINE_ORDER,
    JOB_ORDER,
)

from engine.api.api_system import ROUTE_SPECS_SYSTEM
from engine.api.api_jobs import ROUTE_SPECS_JOBS
from engine.api.api_ops import ROUTE_SPECS_OPS


# ---------------------------------------------------
# ENV CONFIG
# ---------------------------------------------------

host = os.environ.get("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
port = int(os.environ.get("DASHBOARD_PORT", "8000"))

AUTO_BOOT_DAEMONS = os.environ.get("AUTO_BOOT_DAEMONS", "0") == "1"
AUTO_BOOT_TARGETS = [
    x.strip() for x in os.environ.get("AUTO_BOOT_TARGETS", "").split(",")
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
# API HANDLERS
# ---------------------------------------------------

def api_get_health(_parsed):
    return get_health_snapshot()


def api_get_schema_audit(_parsed):
    return get_schema_audit()


def api_post_pipeline_run(_parsed, _body):
    return ORCHESTRATOR.run_pipeline()


API_HANDLERS = {
    "api_get_health": api_get_health,
    "api_get_schema_audit": api_get_schema_audit,
    "api_post_pipeline_run": api_post_pipeline_run,
}


# ---------------------------------------------------
# ROUTE TABLE
# ---------------------------------------------------

ROUTE_SPECS = []
ROUTE_SPECS.extend(ROUTE_SPECS_SYSTEM or [])
ROUTE_SPECS.extend(ROUTE_SPECS_JOBS or [])
ROUTE_SPECS.extend(ROUTE_SPECS_OPS or [])


# ---------------------------------------------------
# HTTP HANDLER
# ---------------------------------------------------

class Handler:

    def __init__(self, request, client_address, server):
        self._inner = server._handler_cls(request, client_address, server)


# ---------------------------------------------------
# SERVER
# ---------------------------------------------------

def run_server():

    try:
        run_preflight()
    except Exception:
        pass

    if AUTO_BOOT_DAEMONS and AUTO_BOOT_TARGETS:
        try:
            SUPERVISOR.deterministic_start(
                AUTO_BOOT_TARGETS,
                include_deps=True,
                strict=False,
            )
        except Exception:
            pass

    httpd = HTTPServer((host, port), server._handler_cls)
    print(f"Dashboard running at http://{host}:{port}")

    try:
        httpd.serve_forever()
    finally:
        try:
            JOBS.stop_all()
        except Exception:
            pass
