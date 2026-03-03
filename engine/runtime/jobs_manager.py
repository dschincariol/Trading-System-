# jobs_manager.py
import os
import sys
import time
import threading
import subprocess
from collections import deque
from typing import Deque, Dict, Optional

from engine.runtime.job_registry import ALLOWED_JOBS, JOB_ORDER

from engine.runtime.config import (
    AUTO_RESTART_DAEMONS,
    DAEMON_RESTART_BASE_DELAY_MS,
    DAEMON_RESTART_MAX_DELAY_MS,
    DAEMON_RESTART_WINDOW_S,
    DAEMON_RESTART_MAX_IN_WINDOW,
    DAEMON_WATCHDOG_PERIOD_S,
    PREFLIGHT_ENABLE,
    PREFLIGHT_BLOCK_JOBS,
)

_DAEMON_STALL_AFTER_MS = int(os.environ.get("DAEMON_STALL_AFTER_MS", "120000"))

from engine.runtime.gates import execution_gate_snapshot

print("JOBS_MANAGER LOADED FROM:", __file__)

# ---------------------------------------------------
# PATHS (robust against wrong CWD)
# jobs_manager.py lives at: engine/runtime/jobs_manager.py
# project root is 2 levels up from engine/runtime/
# ---------------------------------------------------
_ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_PROJECT_ROOT = os.path.abspath(os.path.join(_ENGINE_DIR, ".."))

# ------------------------------
# SQLITE LOCKS + JOB HISTORY (single source of truth)
# ------------------------------
from engine.runtime.locks import (
    ensure_job_locks as _ensure_job_locks,
    acquire_lock as _acquire_lock,
    touch_lock as _touch_lock,
    heartbeat_lock as _heartbeat_lock,
    read_lock as _read_lock,
    release_lock as _release_lock,
    ensure_job_history as _ensure_job_history,
    write_job_history as _write_job_history,
    read_job_history as _read_job_history,
)


# ------------------------------
# PUBLIC READ HELPERS (API)
# ------------------------------

def get_job_log(job_name: str, tail: int = 200) -> str:
    jm = _GLOBAL_JOB_MANAGER.get()
    if not jm:
        return ""
    job = jm.get(job_name)
    if not job:
        return ""
    return job.tail(int(tail or 0))


def get_job_history(job_name: str, limit: int = 200) -> list:
    return _read_job_history(job_name, limit=limit)

# ------------------------------
# JOB STATE / MANAGER
# ------------------------------

class JobState:
    def __init__(self, name: str, script: str, mode: str, group: str = None):
        self.name = name
        self.script = script
        self.mode = mode
        self.group = group
        self.proc: Optional[subprocess.Popen] = None
        self.started_at_ms: Optional[int] = None
        self.exited_at_ms: Optional[int] = None
        self.exit_code: Optional[int] = None
        self.log: Deque[str] = deque(maxlen=4000)
        self._lock = threading.Lock()

        self.stop_requested: bool = False
        self.restart_attempts_window: Deque[int] = deque(maxlen=50)
        self.next_restart_ms: int = 0
        self._restart_in_flight: bool = False
        self.last_start_args: Optional[list] = None
        self.last_start_cwd: Optional[str] = None

        # For oneshot jobs, we hold a cross-process lock "job:<name>"
        self._oneshot_lock_name: Optional[str] = None

    def to_dict(self) -> Dict:
        with self._lock:
            running = self.proc is not None and self.proc.poll() is None
            return {
                "name": self.name,
                "script": self.script,
                "mode": self.mode,
                "group": self.group,
                "running": bool(running),
                "started_at_ms": self.started_at_ms,
                "exited_at_ms": self.exited_at_ms,
                "exit_code": self.exit_code,
                "log_lines": len(self.log),
                "stop_requested": bool(self.stop_requested),
                "next_restart_ms": int(self.next_restart_ms or 0),
            }

    def append_log(self, line: str) -> None:
        with self._lock:
            self.log.append(line.rstrip("\n"))

    def tail(self, n: int) -> str:
        with self._lock:
            if n <= 0:
                return ""
            return "\n".join(list(self.log)[-n:])

# ------------------------------
# GLOBAL JOB MANAGER HANDLE
# ------------------------------

class _GlobalJobManager:
    def __init__(self):
        self._jm = None

    def set(self, jm):
        self._jm = jm

    def get(self):
        return self._jm


_GLOBAL_JOB_MANAGER = _GlobalJobManager()

class JobManager:

    def __init__(
        self,
        preflight_fn=None,
        get_kill_switches_fn=None,
        get_execution_mode_fn=None,
    ):
        self._jobs: Dict[str, JobState] = {}

        for name, value in ALLOWED_JOBS.items():
            # Supported formats:
            # (script, mode)
            # (script, mode, group)
            # (script, mode, group, meta)

            script = None
            mode = None
            group = None
            meta = {}

            if isinstance(value, (list, tuple)):
                if len(value) == 2:
                    script, mode = value
                elif len(value) == 3:
                    script, mode, group = value
                elif len(value) >= 4:
                    script, mode, group, meta = value[0], value[1], value[2], value[3]
                else:
                    continue
            else:
                continue

            js = JobState(name, script, mode, group)
            js.meta = dict(meta or {})
            self._jobs[name] = js

        self._lock = threading.Lock()
        self._preflight_fn = preflight_fn

        # Execution gating providers (injected by runtime / dashboard)
        # If not provided, execution jobs fail-closed by default (safer).
        self._get_kill_switches_fn = get_kill_switches_fn
        self._get_execution_mode_fn = get_execution_mode_fn

        _GLOBAL_JOB_MANAGER.set(self)

        # Ensure DB coordination tables exist early (best-effort)
        try:
            _ensure_job_locks()
        except Exception:
            pass
        try:
            _ensure_job_history()
        except Exception:
            pass
        # -------------------------------------------------
        # WATCHDOG BOOTSTRAP (inline, no dynamic binding)
        # -------------------------------------------------
        if not getattr(self, "_watchdog_started", False):
            self._watchdog_started = True
            t = threading.Thread(
                target=self._daemon_watchdog_loop,
                daemon=True,
            )
            t.start()

        # NOTE:
        # Do NOT auto-start daemons here.
        # Deterministic boot is handled by RuntimeSupervisor
        # inside dashboard_server.run_server().


    def start_initial_daemons(self):
        """
        Start all daemon jobs once at boot.
        NOTE: deterministic boot should be handled by RuntimeSupervisor.
        This is kept only for backwards compatibility.
        """
        order = list(JOB_ORDER or [])
        if not order:
            # fallback: stable deterministic order
            order = sorted(list(self._jobs.keys()))

        for name in order:
            job = self.get(name)
            if not job:
                continue
            if job.mode != "daemon":
                continue
            try:
                self.start(name)
            except Exception:
                pass

    def list_jobs(self):
        with self._lock:
            out = []
            seen = set()

            for name in JOB_ORDER:
                if name in self._jobs:
                    out.append(self._jobs[name].to_dict())
                    seen.add(name)

            for name in sorted(self._jobs):
                if name in seen:
                    continue
                out.append(self._jobs[name].to_dict())

            return out

    def get(self, name: str) -> Optional[JobState]:
        with self._lock:
            return self._jobs.get(name)

    # -------------------------------------------------
    # Compatibility API for dashboard job log/history
    # ctx["JOBS"] is a JobManager in api_handlers
    # -------------------------------------------------
    def get_job_log(self, name: str, tail: int = 200) -> Dict:
        job = self.get(name)
        if not job:
            return {"ok": False, "error": "job_not_found", "job": str(name)}

        try:
            text = job.tail(int(tail or 0))
            lines = text.split("\n") if text else []
            return {"ok": True, "job": str(name), "lines": lines}
        except Exception as e:
            return {"ok": False, "error": "job_log_exception", "detail": str(e), "job": str(name)}

    def get_job_history(self, name: str, limit: int = 200) -> Dict:
        try:
            rows = _read_job_history(str(name or ""), limit=int(limit or 0))
            return {"ok": True, "job": str(name), "rows": rows}
        except Exception as e:
            return {"ok": False, "error": "job_history_exception", "detail": str(e), "job": str(name)}
        
        
    def is_running(self, name: str) -> bool:
        j = self.get(name)
        if not j:
            return False
        p = j.proc
        if not p:
            return False
        try:
            return p.poll() is None
        except Exception:
            return False

    def start(self, name: str) -> Dict:
        job = self.get(name)
        if not job:
            return {"ok": False, "error": f"unknown job: {name}"}

        if (
            PREFLIGHT_ENABLE
            and PREFLIGHT_BLOCK_JOBS
            and self._preflight_fn
            and getattr(job, "meta", {}).get("execution") is True
        ):
            p = self._preflight_fn()
            if not p.get("ok"):
                return {"ok": False, "error": "preflight_failed", "notes": p.get("notes", [])}

        # --------------------------------------------------
        # HARD EXECUTION GATE (cannot be bypassed anywhere)
        # --------------------------------------------------
        if getattr(job, "meta", {}).get("execution") is True:

            fail_open = os.environ.get("EXECUTION_GATE_FAIL_OPEN_IF_NO_PROVIDERS", "0") == "1"

            if not self._get_kill_switches_fn or not self._get_execution_mode_fn:
                if not fail_open:
                    return {
                        "ok": False,
                        "error": "execution_blocked_gate_providers_missing",
                        "job": str(name),
                    }
            else:
                gate = execution_gate_snapshot(
                    system_state=self._get_execution_mode_fn() if self._get_execution_mode_fn else None,
                    kill_switches=self._get_kill_switches_fn() if self._get_kill_switches_fn else None,
                    execution_degraded=False,
                )

                if (not gate.get("ok")) or (not gate.get("allowed")):
                    return {
                        "ok": False,
                        "error": "execution_blocked",
                        "job": str(name),
                        "gate": gate,
                    }

        with job._lock:
            job.stop_requested = False

            if job.proc and job.proc.poll() is None:
                return {"ok": True, "status": "already_running"}

            if job.mode == "daemon":
                # Only enforce exclusivity within the same daemon group (e.g. price_feed).
                # If job.group is None, do not enforce exclusivity.
                if getattr(job, "group", None):
                    for j in self._jobs.values():
                        if (
                            j is not job
                            and j.mode == "daemon"
                            and getattr(j, "group", None) == getattr(job, "group", None)
                            and j.proc
                            and j.proc.poll() is None
                        ):
                            # Deterministic replacement: stop existing daemon in group
                            try:
                                j.append_log(f"[server] stopping due to group replacement by {job.name}")
                                _write_job_history(j.name, "group_replaced", f"replaced by {job.name}", None)
                                j.proc.terminate()
                            except Exception:
                                pass

            if job.mode == "oneshot":
                lock_name = f"job:{job.name}"
                # TTL is extended while running via _pump_output loop
                if not _acquire_lock(lock_name, ttl_ms=10 * 60 * 1000):
                    return {"ok": False, "error": f"job locked: {job.name}"}
                job._oneshot_lock_name = lock_name

            job.exited_at_ms = None
            job.exit_code = None

            py = sys.executable

            # Resolve scripts from the project root (robust even if CWD is wrong)
            script_rel = str(job.script or "")
            script_path = os.path.abspath(os.path.join(_PROJECT_ROOT, script_rel))

            args = [py, "-u", script_path]

            if not os.path.exists(script_path):
                if job.mode == "oneshot":
                    _release_lock(f"job:{job.name}")
                job.append_log(f"[server] script not found: {script_path} (from {script_rel})")
                _write_job_history(
                    job.name,
                    "start_failed",
                    f"script not found: {script_path} (from {script_rel})",
                    None,
                )
                return {"ok": False, "error": f"script not found: {script_path}"}

            job.append_log(f"[server] starting: {args}")
            _write_job_history(job.name, "start", f"{args}", None)

            job.started_at_ms = int(time.time() * 1000)
            job.last_start_args = list(args)
            job.last_start_cwd = str(_PROJECT_ROOT)

            env = dict(os.environ)
            env["ENGINE_LAUNCHED_BY_SUPERVISOR"] = "1"
            env["ENGINE_SUPERVISED"] = "1"
            env["ENGINE_JOB_NAME"] = str(job.name)

            try:
                job.proc = subprocess.Popen(
                    args,
                    cwd=_PROJECT_ROOT,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0),
                )
            except Exception as e:
                if job.mode == "oneshot":
                    try:
                        _release_lock(f"job:{job.name}")
                    except Exception:
                        pass
                job.append_log(f"[server] spawn failed: {e}")
                _write_job_history(job.name, "start_failed", f"spawn failed: {e}", None)
                return {"ok": False, "error": f"spawn failed: {e}"}

            # best-effort heartbeat stamp (locks.py is single source of truth)
            try:
                _heartbeat_lock(f"job:{job.name}")
            except Exception:
                pass

            threading.Thread(target=self._pump_output, args=(job,), daemon=True).start()
            return {"ok": True, "status": "started"}

    def stop(self, name: str) -> Dict:
        job = self.get(name)
        if not job:
            return {"ok": False, "error": f"unknown job: {name}"}

        with job._lock:
            job.stop_requested = True
            job.next_restart_ms = 0

            if not job.proc or job.proc.poll() is not None:
                _write_job_history(job.name, "stop", "not_running", None)
                return {"ok": True, "status": "not_running"}

            job.append_log("[server] stopping...")
            _write_job_history(job.name, "stop", "terminate()", None)

            try:
                job.proc.terminate()
            except Exception as e:
                job.append_log(f"[server] terminate error: {e}")
                _write_job_history(job.name, "stop_failed", str(e), None)
                return {"ok": False, "error": str(e)}

        return {"ok": True, "status": "terminate_sent"}

    def stop_all(self) -> Dict:
        stopped = []
        errors = []

        with self._lock:
            jobs = list(self._jobs.values())

        for job in jobs:
            try:
                self.stop(job.name)
                stopped.append(job.name)
            except Exception as e:
                errors.append(f"{job.name}: {e}")

        deadline = time.time() + 3.0
        for job in jobs:
            try:
                with job._lock:
                    p = job.proc
                if not p:
                    continue
                while time.time() < deadline:
                    if p.poll() is not None:
                        break
                    time.sleep(0.05)
                if p.poll() is None:
                    try:
                        p.kill()
                        with job._lock:
                            job.append_log("[server] hard-kill (kill())")
                            _write_job_history(job.name, "stop_hard_kill", "kill()", None)
                    except Exception as e:
                        errors.append(f"{job.name}: kill failed: {e}")
            except Exception as e:
                errors.append(f"{job.name}: wait/kill error: {e}")

        return {"ok": len(errors) == 0, "stopped": stopped, "errors": errors}

    def _pump_output(self, job: JobState):
        proc = job.proc
        if not proc:
            return

        # For oneshot jobs, keep the lock alive while the process runs
        lock_name = getattr(job, "_oneshot_lock_name", None)

        if not proc.stdout:
            # still wait for exit + release lock
            try:
                rc = proc.wait()
            except Exception:
                rc = proc.poll()
            with job._lock:
                job.exited_at_ms = int(time.time() * 1000)
                job.exit_code = int(rc) if rc is not None else None
                job.append_log(f"[server] exited rc={job.exit_code}")
                _write_job_history(job.name, "exit", "process exited", job.exit_code)
            if lock_name:
                try:
                    _release_lock(lock_name)
                except Exception:
                    pass
                with job._lock:
                    job._oneshot_lock_name = None
                    pass
            return
        try:
            last_hb = 0.0
            for line in proc.stdout:
                if not line:
                    break
                job.append_log(line)

                # keep oneshot lock alive (every ~5s)
                if lock_name:
                    now_s = time.time()
                    if (now_s - last_hb) >= 5.0:
                        last_hb = now_s
                        try:
                            _heartbeat_lock(lock_name)
                        except Exception:
                            pass
                            last_hb = 0.0
            for line in proc.stdout:
                if not line:
                    break
                job.append_log(line)

                # keep oneshot lock alive (every ~5s)
                if lock_name:
                    now_s = time.time()
                    if (now_s - last_hb) >= 5.0:
                        last_hb = now_s
                        try:
                            _heartbeat_lock(lock_name)
                        except Exception:
                            pass
        except Exception as e:
            job.append_log(f"[server] log pump error: {e}")
        finally:
            try:
                rc = proc.poll()
                if rc is None:
                    rc = proc.wait(timeout=1)
            except Exception:
                rc = proc.poll()

            with job._lock:
                job.exited_at_ms = int(time.time() * 1000)
                job.exit_code = int(rc) if rc is not None else None
                job.append_log(f"[server] exited rc={job.exit_code}")
                _write_job_history(job.name, "exit", "process exited", job.exit_code)

            if job.mode == "oneshot":
                _release_lock(f"job:{job.name}")

    def _daemon_watchdog_loop(self):
        while True:
            try:
                if AUTO_RESTART_DAEMONS:
                    self._check_and_restart_daemons()
            except Exception:
                pass
            time.sleep(DAEMON_WATCHDOG_PERIOD_S)

    def _check_and_restart_daemons(self):
        now = int(time.time() * 1000)
        with self._lock:
            jobs = list(self._jobs.values())

        for job in jobs:
            if job.mode != "daemon":
                continue

            with job._lock:
                if job.stop_requested:
                    continue

                if self.is_running(job.name):
                    # heartbeat while healthy
                    try:
                        _heartbeat_lock(f"job:{job.name}")
                    except Exception:
                        pass

                    # stall detection: if heartbeat_ts_ms is not advancing, restart daemon
                    try:
                        row = _read_lock(f"job:{job.name}") or {}
                        hb = int(row.get("heartbeat_ts_ms") or 0)
                        if hb > 0 and (now - hb) > int(_DAEMON_STALL_AFTER_MS):
                            job.append_log(f"[server] daemon stall detected; hb_age_ms={now-hb} > {_DAEMON_STALL_AFTER_MS}; forcing restart")
                            _write_job_history(job.name, "autorestart_stall_detected", f"hb_age_ms={now-hb}", None)

                            # Force-kill without setting stop_requested (so watchdog can restart)
                            try:
                                p = job.proc
                            except Exception:
                                p = None

                            try:
                                if p and p.poll() is None:
                                    try:
                                        p.terminate()
                                    except Exception:
                                        pass
                                    # short wait then kill
                                    deadline = time.time() + 2.0
                                    while time.time() < deadline:
                                        if p.poll() is not None:
                                            break
                                        time.sleep(0.05)
                                    if p.poll() is None:
                                        try:
                                            p.kill()
                                        except Exception:
                                            pass
                            except Exception:
                                pass

                            # allow restart path to proceed
                            job.exit_code = -9
                            job.exited_at_ms = now
                    except Exception:
                        pass

                    continue

                if not job.started_at_ms:
                    continue

                # --------------------------------------------------
                # HARD EXECUTION GATE: never auto-restart execution jobs
                # unless the execution gate is explicitly OK.
                # Fail-closed by default.
                # --------------------------------------------------
                if getattr(job, "meta", {}).get("execution") is True:
                    gate = execution_gate_snapshot(
                        system_state=self._get_execution_mode_fn() if self._get_execution_mode_fn else None,
                        kill_switches=self._get_kill_switches_fn() if self._get_kill_switches_fn else None,
                        execution_degraded=False,
                    )
                    if (not gate.get("ok")) or (not gate.get("allowed")):
                        job.append_log(
                            f"[server] auto-restart blocked (execution gated): {gate.get('reason') or gate}"
                        )
                        _write_job_history(
                            job.name,
                            "autorestart_blocked_execution_gated",
                            str(gate),
                            job.exit_code,
                        )
                        # stop further restart attempts until an operator manually starts
                        job.stop_requested = True
                        continue

                if job.next_restart_ms and now < job.next_restart_ms:
                    continue

                window_start = now - (DAEMON_RESTART_WINDOW_S * 1000)
                while job.restart_attempts_window and job.restart_attempts_window[0] < window_start:
                    job.restart_attempts_window.popleft()

                if len(job.restart_attempts_window) >= DAEMON_RESTART_MAX_IN_WINDOW:
                    job.append_log(
                        f"[server] auto-restart disabled: too many restarts in {DAEMON_RESTART_WINDOW_S}s"
                    )
                    _write_job_history(
                        job.name,
                        "autorestart_blocked",
                        f"too many restarts in {DAEMON_RESTART_WINDOW_S}s",
                        job.exit_code,
                    )
                    job.stop_requested = True
                    continue

                attempt_n = len(job.restart_attempts_window)
                delay = DAEMON_RESTART_BASE_DELAY_MS * (2 ** attempt_n)
                delay = min(int(delay), int(DAEMON_RESTART_MAX_DELAY_MS))
                job.next_restart_ms = now + delay

                # mark restart thread as pending (prevents duplicate threads)
                if job._restart_in_flight:
                    continue
                job._restart_in_flight = True

                job.append_log(f"[server] daemon crashed; scheduling restart in {delay}ms")
                _write_job_history(job.name, "autorestart_scheduled", f"delay_ms={delay}", job.exit_code)

                # record attempt at schedule-time to avoid thread storms
                job.restart_attempts_window.append(int(time.time() * 1000))

            def _restart_later(jref: JobState, delay_ms: int):
                time.sleep(delay_ms / 1000.0)

                with jref._lock:
                    if jref.stop_requested:
                        jref._restart_in_flight = False
                        return
                    if self.is_running(jref.name):
                        jref._restart_in_flight = False
                        return

                res = self.start(jref.name)
                if not res.get("ok"):
                    with jref._lock:
                        jref._restart_in_flight = False
                        jref.append_log(f"[server] auto-restart failed: {res.get('error')}")
                        _write_job_history(jref.name, "autorestart_failed", str(res.get("error") or ""), jref.exit_code)
                    return

                with jref._lock:
                    jref.next_restart_ms = 0
                    jref._restart_in_flight = False
                    jref.append_log("[server] auto-restart: started")
                    _write_job_history(jref.name, "autorestart_started", "started", None)

            threading.Thread(
                target=_restart_later,
                args=(job, delay),
                daemon=True,
            ).start()
