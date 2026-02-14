# jobs_manager.py
import os
import sys
import time
import threading
import subprocess
from collections import deque
from typing import Deque, Dict, Optional

from engine.dev_core.storage import connect as _db_connect
from engine.runtime.job_registry import ALLOWED_JOBS, JOB_ORDER

from dashboard_config import (
    AUTO_RESTART_DAEMONS,
    DAEMON_RESTART_BASE_DELAY_MS,
    DAEMON_RESTART_MAX_DELAY_MS,
    DAEMON_RESTART_WINDOW_S,
    DAEMON_RESTART_MAX_IN_WINDOW,
    DAEMON_WATCHDOG_PERIOD_S,
    PREFLIGHT_ENABLE,
    PREFLIGHT_BLOCK_JOBS,
)

# ------------------------------
# SQLITE-BASED JOB LOCKS (cross-process safe)
# ------------------------------

def _ensure_job_locks():
    con = _db_connect()
    try:
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall()]
        except Exception:
            cols = []

        has_legacy_key = ("key" in cols) and ("job_name" not in cols)

        if has_legacy_key:
            try:
                con.execute("ALTER TABLE job_locks RENAME TO job_locks_legacy")
            except Exception:
                pass
            cols = []

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS job_locks (
              job_name TEXT PRIMARY KEY,
              owner TEXT NOT NULL,
              pid INTEGER NOT NULL,
              acquired_ts_ms INTEGER NOT NULL,
              heartbeat_ts_ms INTEGER NOT NULL,
              expires_ms INTEGER
            )
            """
        )

        if has_legacy_key:
            now = int(time.time() * 1000)
            try:
                legacy_rows = con.execute(
                    "SELECT key, owner, expires_ms FROM job_locks_legacy"
                ).fetchall()
            except Exception:
                legacy_rows = []

            for k, owner, exp in legacy_rows or []:
                con.execute(
                    """
                    INSERT OR REPLACE INTO job_locks
                    (job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms, expires_ms)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        str(k),
                        str(owner or ""),
                        0,
                        int(now),
                        int(now),
                        int(exp) if exp is not None else None,
                    ),
                )

        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall()]
        except Exception:
            cols = []

        def _add(col: str, ddl: str) -> None:
            if col in cols:
                return
            try:
                con.execute(ddl)
            except Exception:
                pass

        _add("job_name", "ALTER TABLE job_locks ADD COLUMN job_name TEXT")
        _add("owner", "ALTER TABLE job_locks ADD COLUMN owner TEXT")
        _add("pid", "ALTER TABLE job_locks ADD COLUMN pid INTEGER")
        _add("acquired_ts_ms", "ALTER TABLE job_locks ADD COLUMN acquired_ts_ms INTEGER")
        _add("heartbeat_ts_ms", "ALTER TABLE job_locks ADD COLUMN heartbeat_ts_ms INTEGER")
        _add("expires_ms", "ALTER TABLE job_locks ADD COLUMN expires_ms INTEGER")

        con.commit()
    finally:
        con.close()

def _acquire_lock(name: str, ttl_ms: int = 10_000) -> bool:
    con = _db_connect()
    try:
        now = int(time.time() * 1000)
        exp = int(now + int(ttl_ms))

        row = con.execute(
            "SELECT owner, pid, expires_ms FROM job_locks WHERE job_name=?",
            (str(name),),
        ).fetchone()

        if row:
            try:
                cur_exp = int(row[2] or 0)
            except Exception:
                cur_exp = 0
            if cur_exp > now:
                return False

        con.execute(
            """
            INSERT OR REPLACE INTO job_locks
              (job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms, expires_ms)
            VALUES (?,?,?,?,?,?)
            """,
            (str(name), str(owner), int(pid), int(now), int(now), int(exp)),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()

def _touch_lock(name: str, ttl_ms: int = 10_000) -> None:
    con = _db_connect()
    try:
        now = int(time.time() * 1000)
        exp = int(now + int(ttl_ms))
        con.execute(
            "UPDATE job_locks SET expires_ms=? WHERE job_name=?",
            (int(exp), str(name)),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()

def _heartbeat_lock(job_name: str, ttl_ms: int = 60_000) -> None:
    _touch_lock(job_name, ttl_ms=ttl_ms)

    _ensure_job_locks()
    now = int(time.time() * 1000)
    owner = f"{os.getpid()}:{threading.get_ident()}"
    pid = int(os.getpid())

    con = _db_connect()
    try:
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall() or []]
        except Exception:
            cols = []

        if "heartbeat_ts_ms" in cols:
            con.execute(
                "UPDATE job_locks SET heartbeat_ts_ms=?, owner=?, pid=? WHERE job_name=?",
                (int(now), str(owner), int(pid), str(job_name)),
            )
        else:
            if "acquired_ts_ms" in cols:
                con.execute(
                    "UPDATE job_locks SET acquired_ts_ms=?, owner=?, pid=? WHERE job_name=?",
                    (int(now), str(owner), int(pid), str(job_name)),
                )
        con.commit()
    finally:
        con.close()

def _release_lock(job_name: str) -> None:
    _ensure_job_locks()
    con = _db_connect()
    try:
        con.execute("DELETE FROM job_locks WHERE job_name=?", (str(job_name),))
        con.commit()
    finally:
        con.close()

# ------------------------------
# JOB HISTORY
# ------------------------------

def _ensure_job_history():
    con = _db_connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS job_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              job_name TEXT NOT NULL,
              event TEXT NOT NULL,
              detail TEXT,
              exit_code INTEGER
            )
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_history_job_ts
              ON job_history(job_name, ts_ms)
            """
        )
        con.commit()
    finally:
        con.close()

def _write_job_history(
    job_name: str,
    event: str,
    detail: str = "",
    exit_code: int = None,
    ts_ms: int = None,
) -> None:
    try:
        _ensure_job_history()
    except Exception:
        pass

    con = _db_connect()
    try:
        now = int(ts_ms or (time.time() * 1000))
        con.execute(
            """
            INSERT INTO job_history(ts_ms, job_name, event, detail, exit_code)
            VALUES (?,?,?,?,?)
            """,
            (
                int(now),
                str(job_name or ""),
                str(event or ""),
                str(detail or ""),
                (int(exit_code) if exit_code is not None else None),
            ),
        )

        try:
            max_rows = int(os.environ.get("JOB_HISTORY_MAX_ROWS", "20000"))
        except Exception:
            max_rows = 20000

        if max_rows > 0:
            con.execute(
                "DELETE FROM job_history WHERE id NOT IN (SELECT id FROM job_history ORDER BY ts_ms DESC LIMIT ?)",
                (int(max_rows),),
            )

        con.commit()
    finally:
        con.close()

def _read_job_history(job_name: str, limit: int = 200) -> list:
    _ensure_job_history()
    con = _db_connect()
    try:
        rows = con.execute(
            """
            SELECT ts_ms, event, detail, exit_code
            FROM job_history
            WHERE job_name=?
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (str(job_name or ""), int(limit)),
        ).fetchall()
        out = []
        for ts_ms, event, detail, exit_code in rows or []:
            out.append(
                {
                    "ts_ms": int(ts_ms or 0),
                    "event": str(event or ""),
                    "detail": str(detail or ""),
                    "exit_code": (int(exit_code) if exit_code is not None else None),
                }
            )
        return out
    finally:
        con.close()

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
        self.last_start_args: Optional[list] = None
        self.last_start_cwd: Optional[str] = None

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

    def __init__(self, preflight_fn=None):
        self._jobs: Dict[str, JobState] = {
            name: JobState(name, script, mode)
            for name, (script, mode) in ALLOWED_JOBS.items()
        }
        self._lock = threading.Lock()
        self._preflight_fn = preflight_fn

        self._watchdog_started = False
        self._start_watchdog_once()

    def _start_watchdog_once(self):
        if self._watchdog_started:
            return
        self._watchdog_started = True
        t = threading.Thread(target=self._daemon_watchdog_loop, daemon=True)
        t.start()

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

        if PREFLIGHT_ENABLE and PREFLIGHT_BLOCK_JOBS and self._preflight_fn:
            p = self._preflight_fn()
            if not p.get("ok"):
                return {"ok": False, "error": "preflight_failed", "notes": p.get("notes", [])}

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
                            return {"ok": False, "error": f"daemon already running in group '{job.group}': {j.name}"}

            if job.mode == "oneshot":
                if not _acquire_lock(f"job:{job.name}", ttl_ms=10 * 60 * 1000):
                    return {"ok": False, "error": f"job locked: {job.name}"}

            job.exited_at_ms = None
            job.exit_code = None

            py = sys.executable
            args = [py, "-u", job.script]

            if not os.path.exists(job.script):
                if job.mode == "oneshot":
                    _release_lock(f"job:{job.name}")
                job.append_log(f"[server] script not found: {job.script}")
                _write_job_history(job.name, "start_failed", f"script not found: {job.script}", None)
                return {"ok": False, "error": f"script not found: {job.script}"}

            job.append_log(f"[server] starting: {args}")
            _write_job_history(job.name, "start", f"{args}", None)

            job.started_at_ms = int(time.time() * 1000)
            job.last_start_args = list(args)
            job.last_start_cwd = os.getcwd()

            job.proc = subprocess.Popen(
                args,
                cwd=os.getcwd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0),
            )

            try:
                con_hb = _db_connect()
                con_hb.execute(
                    "UPDATE job_locks SET heartbeat_ts_ms=? WHERE job_name=?",
                    (int(time.time() * 1000), f"job:{job.name}"),
                )
                con_hb.commit()
            finally:
                try:
                    con_hb.close()
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
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                if not line:
                    break
                job.append_log(line)
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
                    try:
                        _heartbeat_lock(f"job:{job.name}")
                    except Exception:
                        pass
                    continue

                if not job.started_at_ms:
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

                job.append_log(f"[server] daemon crashed; scheduling restart in {delay}ms")
                _write_job_history(job.name, "autorestart_scheduled", f"delay_ms={delay}", job.exit_code)

            time.sleep(delay / 1000.0)

            with job._lock:
                if job.stop_requested:
                    continue
                if self.is_running(job.name):
                    continue

            res = self.start(job.name)
            if not res.get("ok"):
                with job._lock:
                    job.append_log(f"[server] auto-restart failed: {res.get('error')}")
                    _write_job_history(job.name, "autorestart_failed", str(res.get("error") or ""), job.exit_code)
                continue

            with job._lock:
                job.restart_attempts_window.append(int(time.time() * 1000))
                job.next_restart_ms = 0
                job.append_log("[server] auto-restart: started")
                _write_job_history(job.name, "autorestart_started", "started", None)
