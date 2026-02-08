# CREATE NEW FILE: api_jobs.py
# Route specs for job control + pipeline endpoints.
# This file contains only route metadata (no runtime imports from dashboard_server.py).

ROUTE_SPECS = [
    ("GET", "/api/jobs/log", "api_get_job_log"),
    ("GET", "/api/jobs/history", "api_get_job_history"),
    ("GET", "/api/jobs", "api_get_jobs"),
    ("POST", "/api/jobs/start", "api_post_job_start"),
    ("POST", "/api/jobs/stop", "api_post_job_stop"),
    ("POST", "/api/pipeline/run", "api_post_pipeline_run"),
]

ROUTE_SPECS_JOBS = ROUTE_SPECS
