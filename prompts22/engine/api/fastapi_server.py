"""
FastAPI shim layer.

Purpose:
- Retain FastAPI compatibility.
- Delegate all logic to engine/api layer.
- No business logic here.
- No duplicate route definitions.
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from engine.api.api_system import api_get_system_state
from engine.api.api_handlers import (
    api_get_jobs,
    api_start_job,
    api_stop_job,
    api_get_job_log,
    api_get_job_history,
    api_get_health,
    api_run_pipeline,
    api_model_diagnostics,
    api_confidence_mass,
)
from engine.api.api_ops_handlers import api_get_ai_ops_explain

from engine.api.server import JOBS, SUPERVISOR

app = FastAPI()


@app.get("/api/system/state")
def get_system_state():
    return api_get_system_state(JOBS)


@app.get("/api/jobs")
def get_jobs():
    return api_get_jobs(JOBS)


@app.post("/api/jobs/start")
def start_job(name: str):
    return api_start_job(JOBS, name)


@app.post("/api/jobs/stop")
def stop_job(name: str):
    return api_stop_job(JOBS, name)


@app.get("/api/jobs/log")
def job_log(name: str, tail: int = 200):
    return api_get_job_log(JOBS, name, tail)


@app.get("/api/jobs/history")
def job_history(name: str, limit: int = 100):
    return api_get_job_history(JOBS, name, limit)


@app.get("/api/health")
def health():
    return api_get_health(JOBS)


@app.post("/api/pipeline/run")
def run_pipeline():
    return api_run_pipeline(SUPERVISOR)


@app.get("/api/model/diagnostics")
def diagnostics():
    return api_model_diagnostics()


@app.get("/api/confidence_mass")
def confidence_mass():
    return api_confidence_mass()


@app.get("/api/ai/ops_explain")
def ai_ops_explain():
    # pass no parsed object
    return api_get_ai_ops_explain(None, JOBS)
