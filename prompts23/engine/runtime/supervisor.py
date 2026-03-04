"""
Unified Runtime Supervisor (DAG-hardened)

Preserves:
- register_job()
- start()
- stop()
- restart()
- stop_all()
- status()
- heartbeat()
- daemon auto-restart (only when NOT delegating to JobManager)
- restart_count tracking
- exit code tracking
- monitor loop (only when NOT delegating)

Adds (structural / no behavior change by default):
- deterministic_start() with strict DAG enforcement (cycle + missing deps)
- optional dependency enforcement on start() via ENV gate
- optional delegation to JobManager (preferred; single process launcher)
- restart backoff + crash-loop guard (only when NOT delegating)

ENV (all optional):
  SUPERVISOR_ENFORCE_DEPS_ON_START=0|1
  SUPERVISOR_MONITOR_WHEN_DELEGATING=0|1
  SUPERVISOR_RESTART_BASE_DELAY_MS=2000
  SUPERVISOR_RESTART_MAX_DELAY_MS=30000
  SUPERVISOR_RESTART_WINDOW_S=120
  SUPERVISOR_RESTART_MAX_IN_WINDOW=5
  SUPERVISOR_MONITOR_PERIOD_S=2.0
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set

from engine.runtime.job_registry import ALLOWED_JOBS, JOB_ORDER, PIPELINE_ORDER


# ============================================================
# Internal State Objects (only used when NOT delegating)
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

        # crash-loop guard
        self._restart_ts: Deque[float] = deque(maxlen=256)
        self._next_restart_allowed_ts: float = 0.0
        self._current_delay_ms: int = 0


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


def _now() -> float:
    return time.time()


# ============================================================
# Runtime Supervisor
# ============================================================

class RuntimeSupervisor:
    def __init__(self, jobs=None):
        self._jobs: Dict[str, JobState] = {}
        self._lock = threading.Lock()

        # Optional delegation layer (preferred): JobManager
        self._delegate = jobs

        # deps default: linear pipeline chain
        self._deps = _default_deps_from_pipeline(list(PIPELINE_ORDER or []))

        # behavior gates
        self._enforce_deps_on_start = os.environ.get("SUPERVISOR_ENFORCE_DEPS_ON_START", "0") == "1"
        self._monitor_when_delegating = os.environ.get("SUPERVISOR_MONITOR_WHEN_DELEGATING", "0") == "1"

        # restart guard (only used when NOT delegating)
        self._restart_base_delay_ms = int(os.environ.get("SUPERVISOR_RESTART_BASE_DELAY_MS", "2000"))
        self._restart_max_delay_ms = int(os.environ.get("SUPERVISOR_RESTART_MAX_DELAY_MS", "30000"))
        self._restart_window_s = int(os.environ.get("SUPERVISOR_RESTART_WINDOW_S", "120"))
        self._restart_max_in_window = int(os.environ.get("SUPERVISOR_RESTART_MAX_IN_WINDOW", "5"))
        self._monitor_period_s = float(os.environ.get("SUPERVISOR_MONITOR_PERIOD_S", "2.0"))

        # monitor thread
        self._monitor_thread = None
        if self._delegate is None:
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
            )
            self._monitor_thread.start()

    # ========================================================
    # Registration (used only when NOT delegating)
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

    def pipeline_order(self) -> List[str]:
        return list(PIPELINE_ORDER or [])

    def status(self) -> Dict[str, Any]:
        """
        Returns status for:
          - delegated JobManager if present (preferred)
          - local supervisor jobs otherwise
        """
        if self._delegate is not None:
            try:
                return {"ok": True, "delegated": True, "jobs": self._delegate.list_jobs()}
            except Exception as e:
                return {"ok": False, "delegated": True, "error": str(e), "jobs": []}

        out = {}
        with self._lock:
            for name, state in self._jobs.items():
                proc = state.process
                running = False
                pid = None
                try:
                    if proc is not None and proc.poll() is None:
                        running = True
                        pid = proc.pid
                except Exception:
                    running = False
                    pid = None

                out[name] = {
                    "name": name,
                    "running": running,
                    "pid": pid,
                    "last_start_ts": state.last_start_ts,
                    "last_exit_code": state.last_exit_code,
                    "restart_count": state.restart_count,
                    "last_heartbeat_ts": state.last_heartbeat_ts,
                }
        return {"ok": True, "delegated": False, "jobs": out}

    def heartbeat(self, name: str) -> None:
        if self._delegate is not None:
            # JobManager heartbeats via job_locks; no-op here
            return
        with self._lock:
            state = self._require(name)
            state.last_heartbeat_ts = _now()

    # ========================================================
    # Control (delegates to JobManager when present)
    # ========================================================

    def start(self, name: str) -> Dict[str, Any]:
        """
        Backwards-compatible: starts a single job.
        Dependency enforcement is OFF by default (gate via SUPERVISOR_ENFORCE_DEPS_ON_START=1).
        """
        if self._delegate is not None:
            if self._enforce_deps_on_start:
                return self.start_with_deps(name, strict=True)
            try:
                return self._delegate.start(name)
            except Exception as e:
                return {"ok": False, "error": str(e)}

        if self._enforce_deps_on_start:
            return self.start_with_deps(name, strict=True)

        with self._lock:
            state = self._require(name)

            if state.process and state.process.poll() is None:
                return {"ok": True, "already_running": True}

            env = os.environ.copy()
            env["ENGINE_SUPERVISED"] = "1"

            state.process = subprocess.Popen(
                ["python", state.spec.script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )

            state.last_start_ts = _now()
            state.last_exit_code = None

            return {"ok": True}

    def stop(self, name: str) -> Dict[str, Any]:
        if self._delegate is not None:
            try:
                return self._delegate.stop(name)
            except Exception as e:
                return {"ok": False, "error": str(e)}

        with self._lock:
            state = self._require(name)
            if state.process and state.process.poll() is None:
                state.process.terminate()
                try:
                    state.process.wait(timeout=5)
                except Exception:
                    try:
                        state.process.kill()
                    except Exception:
                        pass
            state.process = None
            return {"ok": True}

    def restart(self, name: str) -> Dict[str, Any]:
        self.stop(name)
        return self.start(name)

    def stop_all(self) -> Dict[str, Any]:
        if self._delegate is not None:
            try:
                self._delegate.stop_all()
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        with self._lock:
            for name in list(self._jobs.keys()):
                try:
                    self.stop(name)
                except Exception:
                    pass
        return {"ok": True}

    # ========================================================
    # Deterministic Startup (DAG hardened)
    # ========================================================

    def deterministic_start(
        self,
        targets: List[str],
        *,
        include_deps: bool = True,
        strict: bool = True,
    ) -> Dict[str, Any]:

        targets = [str(x).strip() for x in (targets or []) if str(x).strip()]

        # Validate graph against registry
        v = self.validate_graph(strict=strict)
        if not v.get("ok"):
            return {"ok": False, "order": [], "started": [], "errors": list(v.get("errors") or [])}

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
            if not self._is_known_job(name):
                if strict:
                    ok = False
                    errors.append(f"not_registered:{name}")
                continue

            r = self.start(name)
            if not r.get("ok"):
                ok = False
                errors.append(f"start_failed:{name}:{r.get('error') or ''}".strip(":"))
            else:
                started.append(name)

        return {"ok": ok, "order": resolved, "started": started, "errors": errors}

    def start_with_deps(self, name: str, *, strict: bool = True) -> Dict[str, Any]:
        """
        Starts a single job and its deps in topo order.
        Does NOT change start() behavior unless SUPERVISOR_ENFORCE_DEPS_ON_START=1.
        """
        return self.deterministic_start([name], include_deps=True, strict=strict)

    def validate_graph(self, *, strict: bool = True) -> Dict[str, Any]:
        """
        Validates dependency graph:
          - cycle detection
          - missing deps
          - unknown nodes in deps
        Uses ALLOWED_JOBS as the source of truth.
        """
        known = set(ALLOWED_JOBS.keys())
        errors: List[str] = []

        # ensure pipeline names are known
        for n in (PIPELINE_ORDER or []):
            if n not in known:
                errors.append(f"pipeline_unknown_job:{n}")

        # cycle + missing deps
        seen: Set[str] = set()
        visiting: Set[str] = set()

        def dfs(n: str):
            if n in seen:
                return
            if n in visiting:
                errors.append(f"dependency_cycle:{n}")
                return
            visiting.add(n)
            for d in (self._deps.get(n) or []):
                if d not in known:
                    errors.append(f"missing_dep:{n}->{d}")
                    continue
                dfs(d)
            visiting.remove(n)
            seen.add(n)

        for n in list(known):
            dfs(n)

        ok = len(errors) == 0
        if strict and not ok:
            return {"ok": False, "errors": errors}
        return {"ok": True, "errors": errors}

    def _topo_expand(self, targets: List[str], strict: bool = True) -> List[str]:
        order_index = {name: i for i, name in enumerate(list(JOB_ORDER or []))}
        known = set(ALLOWED_JOBS.keys())
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
                if d not in known:
                    if strict:
                        raise RuntimeError(f"missing_dep:{n}->{d}")
                    continue
                visit(d, stack)
            stack.remove(n)
            seen.add(n)
            out.append(n)

        for t in targets:
            if t not in known and strict:
                raise RuntimeError(f"not_registered:{t}")
            visit(t, set())

        return sorted(out, key=lambda n: order_index.get(n, 10**9))

    # ========================================================
    # Internal
    # ========================================================

    def _is_known_job(self, name: str) -> bool:
        try:
            return str(name) in set(ALLOWED_JOBS.keys())
        except Exception:
            return False

    def _require(self, name: str) -> JobState:
        if name not in self._jobs:
            raise ValueError(f"Job not registered: {name}")
        return self._jobs[name]

    # ========================================================
    # Monitor Loop (only active when NOT delegating by default)
    # ========================================================

    def _monitor_loop(self):
        while True:
            time.sleep(self._monitor_period_s)

            # If delegating and monitor not enabled, do nothing.
            if self._delegate is not None and not self._monitor_when_delegating:
                continue

            with self._lock:
                for name, state in self._jobs.items():
                    if not state.process:
                        continue

                    exit_code = None
                    try:
                        exit_code = state.process.poll()
                    except Exception:
                        exit_code = None

                    if exit_code is None:
                        continue

                    # process exited
                    state.last_exit_code = exit_code
                    state.process = None

                    # restart only daemons
                    if not state.spec.daemon:
                        continue

                    now = _now()

                    # crash-loop window: drop old timestamps
                    while state._restart_ts and (now - state._restart_ts[0]) > float(self._restart_window_s):
                        state._restart_ts.popleft()

                    if len(state._restart_ts) >= int(self._restart_max_in_window):
                        # too many restarts in window -> stop restarting
                        continue

                    # backoff timing
                    if now < state._next_restart_allowed_ts:
                        continue

                    if state._current_delay_ms <= 0:
                        state._current_delay_ms = int(self._restart_base_delay_ms)
                    else:
                        state._current_delay_ms = min(int(self._restart_max_delay_ms), int(state._current_delay_ms * 2))

                    delay_s = float(state._current_delay_ms) / 1000.0
                    state._next_restart_allowed_ts = now + delay_s
                    state._restart_ts.append(now)
                    state.restart_count += 1

                    # schedule actual restart after delay (non-blocking)
                    def _restart_later(job_name: str):
                        time.sleep(delay_s)
                        with self._lock:
                            st = self._jobs.get(job_name)
                            if not st:
                                return
                            if st.process is not None:
                                return
                            try:
                                env = os.environ.copy()
                                env["ENGINE_SUPERVISED"] = "1"

                                st.process = subprocess.Popen(
                                    ["python", st.spec.script],
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    env=env,
                                )
                                st.last_start_ts = _now()
                                st.last_exit_code = None
                            except Exception:
                                return

                    threading.Thread(target=_restart_later, args=(name,), daemon=True).start()
