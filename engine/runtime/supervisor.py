"""
Unified Runtime Supervisor

Preserves:
- register_job()
- start()
- stop()
- restart()
- stop_all()
- status()
- heartbeat()
- daemon auto-restart
- restart_count tracking
- exit code tracking
- monitor loop

Adds:
- deterministic_start()
- dependency graph (PIPELINE_ORDER)
- strict mode
- optional JobManager delegation

Safe to replace old file.
"""

from __future__ import annotations

import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Set

from engine.runtime.job_registry import ALLOWED_JOBS, JOB_ORDER, PIPELINE_ORDER


# ============================================================
# Internal State Objects
# ============================================================

class JobSpec:
    def __init__(self, name: str, script: str, daemon: bool = False):
        self.name = name
        self.script = script
        self.daemon = daemon


class JobState:
    def __init__(self, spec: JobSpec):
        self.spec = spec
        self.process: Optional[subprocess.Popen] = None
        self.last_heartbeat_ts: float = 0.0
        self.last_start_ts: float = 0.0
        self.last_exit_code: Optional[int] = None
        self.restart_count: int = 0


# ============================================================
# Dependency Helper
# ============================================================

def _default_deps_from_pipeline(pipeline: List[str]) -> Dict[str, List[str]]:
    deps: Dict[str, List[str]] = {}
    prev: Optional[str] = None
    for name in pipeline or []:
        if prev is None:
            deps.setdefault(name, [])
        else:
            deps.setdefault(name, []).append(prev)
        prev = name
    return deps


# ============================================================
# Runtime Supervisor
# ============================================================

class RuntimeSupervisor:
    def __init__(self, jobs=None):
        self._jobs: Dict[str, JobState] = {}
        self._lock = threading.Lock()

        # Optional delegation layer
        self._delegate = jobs

        self._deps = _default_deps_from_pipeline(list(PIPELINE_ORDER or []))

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True
        )
        self._monitor_thread.start()

    # ========================================================
    # Registration
    # ========================================================

    def register_job(self, name: str, script: str, daemon: bool = False):
        with self._lock:
            if name in self._jobs:
                raise ValueError(f"Job already registered: {name}")
            spec = JobSpec(name=name, script=script, daemon=daemon)
            self._jobs[name] = JobState(spec)

    # ========================================================
    # Introspection
    # ========================================================

    def allowed_jobs(self) -> Dict[str, Any]:
        return dict(ALLOWED_JOBS)

    def job_order(self) -> List[str]:
        return list(JOB_ORDER or [])

    def deps_graph(self) -> Dict[str, List[str]]:
        return {k: list(v or []) for k, v in (self._deps or {}).items()}

    def list_jobs(self) -> List[Dict[str, Any]]:
        return list(self.status().values())

    def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        with self._lock:
            for name, state in self._jobs.items():
                running = (
                    state.process is not None and
                    state.process.poll() is None
                )
                out[name] = {
                    "running": running,
                    "daemon": state.spec.daemon,
                    "last_start_ts": state.last_start_ts,
                    "last_exit_code": state.last_exit_code,
                    "restart_count": state.restart_count,
                    "last_heartbeat_ts": state.last_heartbeat_ts,
                }
        return out

    # ========================================================
    # Control
    # ========================================================

    def start(self, name: str) -> Dict[str, Any]:
        with self._lock:
            state = self._require(name)

            if state.process and state.process.poll() is None:
                return {"ok": True, "already_running": True}

            state.process = subprocess.Popen(
                ["python", state.spec.script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            state.last_start_ts = time.time()
            state.last_exit_code = None

            return {"ok": True}

    def stop(self, name: str) -> Dict[str, Any]:
        with self._lock:
            state = self._require(name)
            if state.process and state.process.poll() is None:
                state.process.terminate()
                try:
                    state.process.wait(timeout=5)
                except Exception:
                    state.process.kill()
            state.process = None
            return {"ok": True}

    def restart(self, name: str) -> Dict[str, Any]:
        self.stop(name)
        return self.start(name)

    def stop_all(self) -> Dict[str, Any]:
        with self._lock:
            for name in list(self._jobs.keys()):
                self.stop(name)
        return {"ok": True}

    # ========================================================
    # Heartbeat
    # ========================================================

    def heartbeat(self, name: str):
        with self._lock:
            state = self._require(name)
            state.last_heartbeat_ts = time.time()

    # ========================================================
    # Deterministic Startup
    # ========================================================

    def deterministic_start(
        self,
        targets: List[str],
        *,
        include_deps: bool = True,
        strict: bool = True,
    ) -> Dict[str, Any]:

        targets = [str(x).strip() for x in (targets or []) if str(x).strip()]

        if include_deps:
            try:
                resolved = self._topo_expand(targets, strict=strict)
            except Exception as e:
                return {"ok": False, "order": [], "started": [], "errors": [str(e)]}
        else:
            resolved = targets

        started = []
        errors = []
        ok = True

        for name in resolved:
            if name not in self._jobs:
                if strict:
                    ok = False
                    errors.append(f"not_registered:{name}")
                continue

            r = self.start(name)
            if not r.get("ok"):
                ok = False
                errors.append(f"start_failed:{name}")
            else:
                started.append(name)

        return {"ok": ok, "order": resolved, "started": started, "errors": errors}

    def _topo_expand(self, targets: List[str], strict: bool = True) -> List[str]:
        order_index = {name: i for i, name in enumerate(list(JOB_ORDER or []))}
        seen: Set[str] = set()
        out: List[str] = []

        def visit(n: str, stack: Set[str]):
            if n in seen:
                return
            if n in stack:
                if strict:
                    raise RuntimeError(f"dependency_cycle:{n}")
                return

            stack.add(n)
            for d in (self._deps.get(n) or []):
                if d not in self._jobs and strict:
                    raise RuntimeError(f"missing_dep:{n}->{d}")
                if d in self._jobs:
                    visit(d, stack)
            stack.remove(n)
            seen.add(n)
            out.append(n)

        for t in targets:
            visit(t, set())

        return sorted(out, key=lambda n: order_index.get(n, 10**9))

    # ========================================================
    # Monitor Loop
    # ========================================================

    def _monitor_loop(self):
        while True:
            time.sleep(2)
            with self._lock:
                for name, state in self._jobs.items():
                    if not state.process:
                        continue

                    exit_code = state.process.poll()

                    if exit_code is not None:
                        state.last_exit_code = exit_code
                        state.process = None

                        if state.spec.daemon:
                            state.restart_count += 1
                            state.process = subprocess.Popen(
                                ["python", state.spec.script],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            state.last_start_ts = time.time()

    # ========================================================
    # Internal
    # ========================================================

    def _require(self, name: str) -> JobState:
        if name not in self._jobs:
            raise ValueError(f"Unknown job: {name}")
        return self._jobs[name]
