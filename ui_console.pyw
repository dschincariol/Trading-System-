"""
ui_console.pyw

Double-click launcher for the local dashboard server (Windows).

- Starts/stops dashboard_server.py without opening a command prompt.
- Shows server stdout in a small GUI console.
- Opens the browser to /ui/dashboard.html

PATCHED FOR (additive, no feature removals):
- Structured readiness display + deterministic boot progress
- Colored boot stage output
- Crash auto-restart + cooldown + crash-window guard
- Per-job grid indicators (green/red)
- Execution mode banner (LIVE / SHADOW) + system state
- Training-mode indicator (best-effort)
"""

import os
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
import json
import urllib.request
from datetime import datetime


# ------------------------------------------------------------
# Structured Boot Stages (NEW)
# ------------------------------------------------------------
BOOT_STAGES = [
    "bootstrap_db",
    "module_schema",
    "backtest_schema",
    "labels",
    "size_policy",
    "server_start",
]

BOOT_COLORS = {
    "bootstrap_db": "#1f6f3b",
    "module_schema": "#1f6f3b",
    "backtest_schema": "#1f6f3b",
    "labels": "#6b4f1f",
    "size_policy": "#6b4f1f",
    "server_start": "#1f6f3b",
    "error": "#7a1f1f",
}

# Crash protection (NEW)
AUTO_RESTART_ON_CRASH = True
AUTO_RESTART_DELAY_MS = 3000
MAX_CRASH_RESTARTS = 3

# Crash-window guard (NEW)
CRASH_WINDOW_S = 60
MAX_CRASHES_IN_WINDOW = 3

# Restart cooldown (NEW)
RESTART_COOLDOWN_S = 8


PORT = int(os.environ.get("DASHBOARD_PORT", "8000"))
URL = f"http://localhost:{PORT}/ui/dashboard.html"
STATUS_URL = f"http://localhost:{PORT}/api/server/status"
SHUTDOWN_URL = f"http://localhost:{PORT}/api/server/shutdown"
JOBS_URL = f"http://localhost:{PORT}/api/jobs"
SYSTEM_STATE_URL = f"http://localhost:{PORT}/api/system/state"
HEALTH_URL = f"http://localhost:{PORT}/api/health"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    os.chdir(BASE_DIR)
except Exception:
    pass

LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def _today_log_path() -> str:
    d = datetime.now().strftime("%Y%m%d")
    return os.path.join(LOG_DIR, f"server_console_{d}.log")


def _prune_old_logs(keep_days: int = 14):
    try:
        files = []
        for name in os.listdir(LOG_DIR):
            if name.startswith("server_console_") and name.endswith(".log"):
                files.append(name)
        files.sort(reverse=True)  # newest first
        for name in files[keep_days:]:
            try:
                os.remove(os.path.join(LOG_DIR, name))
            except Exception:
                pass
    except Exception:
        pass


def _http_json(url: str, timeout_s: float = 1.2):
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else None
    except Exception:
        return None


def find_venv_python() -> str:
    """Prefer a local venv python if present, so jobs run with installed deps."""
    here = os.getcwd()
    candidates = [
        os.path.join(here, ".venv", "Scripts", "python.exe"),
        os.path.join(here, "venv", "Scripts", "python.exe"),
        os.path.join(here, "env", "Scripts", "python.exe"),
        os.path.join(here, ".venv", "Scripts", "pythonw.exe"),
        os.path.join(here, "venv", "Scripts", "pythonw.exe"),
        os.path.join(here, "env", "Scripts", "pythonw.exe"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return sys.executable


def _run_and_stream(app, args, cwd=None, env=None, label="[startup]"):
    """
    Run a command and stream combined stdout/stderr into the UI console.
    Returns (ok: bool).
    """
    try:
        app.after(0, app._log, f"{label} running: {args}\n")
        p = subprocess.Popen(
            args,
            cwd=(cwd or BASE_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0),
        )
        if p.stdout:
            for line in p.stdout:
                if not line:
                    break
                app.after(0, app._log, line)
        rc = p.wait()
        app.after(0, app._log, f"{label} rc={rc}\n")
        return rc == 0
    except Exception as e:
        app.after(0, app._log, f"{label} ERROR: {e}\n")
        return False


def _startup_procedure(app):
    """
    One-button bootstrap for non-technical startup:
      - ensure DB schema exists (including module-owned schemas)
      - (optionally) run size policy training + label exec (dashboard can still do it too)
    """
    py = find_venv_python()

    # 1) Create core DB tables
    app.after(0, app._set_stage, "bootstrap_db")
    ok = _run_and_stream(
        app,
        [py, "-u", "-c", "from engine.runtime.storage import init_db; init_db(); print('[startup] init_db ok')"],
        label="[startup]"
    )
    if not ok:
        return False

    # 2) Create module-owned schemas that preflight expects to exist
    app.after(0, app._set_stage, "module_schema")
    ok = _run_and_stream(
        app,
        [py, "-u", "-c",
         "from engine.strategy.portfolio import init_portfolio_db; "
         "from engine.execution.broker_sim import init_broker_db; "
         "from engine.runtime.alerts import init_alerts_db; "
         "from engine.strategy.validation import init_validation_db; "
         "from engine.strategy.model_v2 import init_model_db; "
         "init_portfolio_db(); init_broker_db(); init_alerts_db(); init_validation_db(); init_model_db(); "
         "print('[startup] module db init ok')"],
        label="[startup]"
    )
    if not ok:
        return False

    # 3) Ensure backtest output tables exist
    app.after(0, app._set_stage, "backtest_schema")
    ok = _run_and_stream(
        app,
        [py, "-u", "-c",
         "import portfolio_backtest as p; "
         "from engine.runtime.storage import connect; "
         "con=connect(); con.executescript(p.SCHEMA); con.commit(); con.close(); "
         "print('[startup] portfolio_backtest schema ok')"],
        label="[startup]"
    )
    if not ok:
        return False

    # 4) Optional: bootstrap exec labels + size policy now
    app.after(0, app._set_stage, "labels")
    _run_and_stream(app, [py, "-u", "compute_exec_labels.py"], label="[startup]")

    app.after(0, app._set_stage, "size_policy")
    _run_and_stream(app, [py, "-u", "train_size_policy.py"], label="[startup]")

    return True


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Market Impact — Dashboard Console")
        self.geometry("1100x680")

        self.proc = None
        self._pump_thread = None

        # crash tracking (NEW)
        self.crash_count = 0
        self._crash_times = []  # epoch seconds
        self._cooldown_until = 0

        top = tk.Frame(self)
        top.pack(fill="x", padx=10, pady=10)

        self.lbl = tk.Label(top, text=URL, anchor="w")
        self.lbl.pack(side="left", fill="x", expand=True)

        self.status_pill = tk.Label(top, text="OFFLINE", padx=10, pady=2, bg="#7a1f1f", fg="white")
        self.status_pill.pack(side="left", padx=(10, 0))

        self.uptime_lbl = tk.Label(top, text="uptime: —", anchor="w")
        self.uptime_lbl.pack(side="left", padx=(10, 0))

        # Execution mode + training indicator (NEW)
        self.mode_lbl = tk.Label(top, text="mode: —", anchor="w")
        self.mode_lbl.pack(side="left", padx=(10, 0))

        self.train_lbl = tk.Label(top, text="training: —", anchor="w")
        self.train_lbl.pack(side="left", padx=(10, 0))

        self.btn_open = tk.Button(top, text="Open Dashboard", command=self.open_browser)
        self.btn_open.pack(side="right", padx=(8, 0))

        self.btn_restart = tk.Button(top, text="Restart", command=self.restart_server, state="disabled")
        self.btn_restart.pack(side="right", padx=(8, 0))

        self.btn_stop = tk.Button(top, text="Stop Server", command=self.stop_server, state="disabled")
        self.btn_stop.pack(side="right", padx=(8, 0))

        self.btn_start = tk.Button(top, text="Start Server", command=self.start_server)
        self.btn_start.pack(side="right")

        self.btn_clear = tk.Button(top, text="Clear Log", command=self.clear_console)
        self.btn_clear.pack(side="right", padx=(8, 0))

        # Boot progress + readiness (NEW)
        prog = tk.Frame(self)
        prog.pack(fill="x", padx=10, pady=(0, 6))

        self.progress = tk.DoubleVar()
        self.progress_bar = tk.Scale(
            prog,
            variable=self.progress,
            from_=0,
            to=len(BOOT_STAGES),
            orient="horizontal",
            state="disabled",
            length=420
        )
        self.progress_bar.pack(side="left")

        self.stage_label = tk.Label(prog, text="Stage: idle", anchor="w")
        self.stage_label.pack(side="left", padx=(10, 0))

        # Jobs grid (NEW)
        grid = tk.LabelFrame(self, text="Jobs (live)")
        grid.pack(fill="x", padx=10, pady=(0, 8))

        self.jobs_frame = tk.Frame(grid)
        self.jobs_frame.pack(fill="x", padx=6, pady=6)

        self._job_rows = {}  # name -> (pill_label, text_label)

        self.txt = tk.Text(self, wrap="none")
        self.txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.txt.tag_config("error", foreground="#7a1f1f")
        self.txt.tag_config("startup", foreground="#1f6f3b")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._log("Ready. Click 'Start Server'.\n")
        self._set_status(False, 0)

        self.after(300, self._poll_status_loop)
        self.after(1200, self._poll_health_loop)
        self.after(1500, self._poll_jobs_loop)
        self.after(1500, self._poll_system_state_loop)
        self.after(500, self._cooldown_tick)

    def _now_s(self) -> int:
        return int(time.time())

    def _set_stage(self, stage_name: str):
        if stage_name not in BOOT_STAGES:
            return
        idx = BOOT_STAGES.index(stage_name) + 1
        self.progress.set(idx)
        self.stage_label.configure(
            text=f"Stage: {stage_name}",
            fg=BOOT_COLORS.get(stage_name, "black")
        )

    def _log(self, s: str):
        if "ERROR" in s or "FAILED" in s:
            self.txt.insert("end", s, "error")
        elif "[startup]" in s:
            self.txt.insert("end", s, "startup")
        else:
            self.txt.insert("end", s)
        self.txt.see("end")
        try:
            _prune_old_logs(keep_days=14)
            with open(_today_log_path(), "a", encoding="utf-8") as f:
                f.write(s)
        except Exception:
            pass

    def open_browser(self):
        webbrowser.open(URL)

    def clear_console(self):
        try:
            self.txt.delete("1.0", "end")
        except Exception:
            pass

    def restart_server(self):
        self._log("[ui] restarting...\n")
        self.stop_server(graceful=True)
        self.after(800, self.start_server)

    def _set_status(self, online: bool, uptime_s: int = 0):
        if online:
            self.status_pill.configure(text="ONLINE", bg="#1f6f3b", fg="white")
            self.uptime_lbl.configure(text=f"uptime: {uptime_s}s")
        else:
            self.status_pill.configure(text="OFFLINE", bg="#7a1f1f", fg="white")
            self.uptime_lbl.configure(text="uptime: —")
            self.mode_lbl.configure(text="mode: —")
            self.train_lbl.configure(text="training: —")

    def _poll_status_loop(self):
        j = _http_json(STATUS_URL)
        if j and j.get("ok"):
            self._set_status(True, int(j.get("uptime_s") or 0))
        else:
            self._set_status(False, 0)
        self.after(1000, self._poll_status_loop)

    def _poll_health_loop(self):
        try:
            j = _http_json(HEALTH_URL)
            if j and j.get("ok"):
                prices_age = j.get("prices_age_s")
                jobs = j.get("jobs") or []
                self.stage_label.configure(
                    text=f"HEALTH: prices_age={prices_age}s jobs={len(jobs)}",
                    fg="#1f6f3b"
                )
            else:
                self.stage_label.configure(text="HEALTH: degraded", fg="#7a1f1f")
        except Exception:
            pass
        self.after(3000, self._poll_health_loop)

    def _poll_system_state_loop(self):
        try:
            j = _http_json(SYSTEM_STATE_URL)
            if j and j.get("ok"):
                # best-effort fields
                state = j.get("state") or j.get("system_state") or j.get("mode") or "—"
                exec_mode = None
                ks = j.get("kill_switches") or {}
                # try common patterns
                em = j.get("execution_mode") or j.get("execution") or {}
                if isinstance(em, dict):
                    exec_mode = em.get("mode") or em.get("state") or em.get("execution_mode")

                if not exec_mode:
                    exec_mode = "SHADOW" if ks.get("kill_switch") or ks.get("kill_switch_enabled") else "LIVE"

                training = j.get("training") or j.get("is_training") or j.get("training_mode")
                training_txt = "on" if training else ("off" if training is not None else "—")

                self.mode_lbl.configure(text=f"state: {state} / exec: {exec_mode}")
                self.train_lbl.configure(text=f"training: {training_txt}")
        except Exception:
            pass
        self.after(2000, self._poll_system_state_loop)

    def _ensure_job_row(self, name: str):
        if name in self._job_rows:
            return

        row = tk.Frame(self.jobs_frame)
        row.pack(fill="x", pady=1)

        pill = tk.Label(row, text="—", width=9, padx=6, pady=1, bg="#7a1f1f", fg="white")
        pill.pack(side="left")

        lab = tk.Label(row, text=name, anchor="w")
        lab.pack(side="left", padx=(8, 0), fill="x", expand=True)

        self._job_rows[name] = (pill, lab)

    def _poll_jobs_loop(self):
        try:
            j = _http_json(JOBS_URL)
            if j and j.get("ok"):
                jobs = j.get("jobs") or []
                # Expect list of dicts; best-effort
                for it in jobs:
                    name = (it.get("name") or it.get("job") or "").strip()
                    if not name:
                        continue
                    self._ensure_job_row(name)

                    status = (it.get("status") or it.get("state") or "").lower()
                    running = bool(it.get("running")) if "running" in it else (status in ("running", "live", "ok"))
                    degraded = status in ("degraded", "stale", "error", "failed")

                    pill, _lab = self._job_rows[name]
                    if degraded:
                        pill.configure(text="DEGRADED", bg="#7a1f1f")
                    elif running:
                        pill.configure(text="RUNNING", bg="#1f6f3b")
                    else:
                        pill.configure(text="STOPPED", bg="#6b4f1f")

        except Exception:
            pass
        self.after(2000, self._poll_jobs_loop)

    def _cooldown_tick(self):
        now = self._now_s()
        if self._cooldown_until > now:
            remain = self._cooldown_until - now
            try:
                self.btn_start.configure(state="disabled")
                self.btn_restart.configure(state="disabled")
            except Exception:
                pass
            self.stage_label.configure(text=f"cooldown: {remain}s", fg="#6b4f1f")
        else:
            # allow UI controls based on proc state
            if not (self.proc and self.proc.poll() is None):
                try:
                    self.btn_start.configure(state="normal")
                    self.btn_restart.configure(state="disabled")
                    self.btn_stop.configure(state="disabled")
                except Exception:
                    pass
        self.after(500, self._cooldown_tick)

    def _crash_window_allows_restart(self) -> bool:
        now = self._now_s()
        # prune
        self._crash_times = [t for t in self._crash_times if (now - t) <= CRASH_WINDOW_S]
        return len(self._crash_times) < MAX_CRASHES_IN_WINDOW

    def start_server(self):
        if self._cooldown_until > self._now_s():
            self._log("[ui] start blocked by cooldown\n")
            return

        if self.proc and self.proc.poll() is None:
            self._log("[ui] server already running\n")
            return

        if not os.path.exists(os.path.join(BASE_DIR, "dashboard_server.py")):
            self._log("[ui] ERROR: dashboard_server.py not found in this folder\n")
            return

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="disabled")
        self.btn_restart.configure(state="disabled")

        def _boot_then_start():
            try:
                self.after(0, self._log, "[ui] startup: bootstrapping environment + database...\n")
                ok = _startup_procedure(self)
                if not ok:
                    self.after(0, self._log, "[ui] startup FAILED — server not started\n")
                    self.after(0, self.btn_start.configure, {"state": "normal"})
                    return

                py = find_venv_python()
                args = [py, "-u", "dashboard_server.py"]

                self.after(0, self._set_stage, "server_start")
                self.after(0, self._log, f"[ui] starting: {args}\n")

                self.proc = subprocess.Popen(
                    args,
                    cwd=BASE_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform.startswith("win") else 0),
                )

                self.after(0, self.btn_stop.configure, {"state": "normal"})
                self.after(0, self.btn_restart.configure, {"state": "normal"})

                self._pump_thread = threading.Thread(target=self._pump, daemon=True)
                self._pump_thread.start()

                self.after(250, self.open_browser)

            except Exception as e:
                self.after(0, self._log, f"[ui] startup exception: {e}\n")
                self.after(0, self.btn_start.configure, {"state": "normal"})

        threading.Thread(target=_boot_then_start, daemon=True).start()

    def stop_server(self, graceful: bool = True):
        if not self.proc or self.proc.poll() is not None:
            self._log("[ui] server not running\n")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.btn_restart.configure(state="disabled")
            return

        self._log("[ui] stopping...\n")

        if graceful:
            j = _http_json(SHUTDOWN_URL)
            if j and j.get("ok"):
                self._log("[ui] graceful shutdown requested\n")

        def _terminate_later():
            try:
                if self.proc and self.proc.poll() is None:
                    self._log("[ui] terminate() fallback\n")
                    self.proc.terminate()
            except Exception as e:
                self._log(f"[ui] terminate error: {e}\n")

        self.after(1200, _terminate_later)

    def _pump(self):
        try:
            if self.proc and self.proc.stdout:
                for line in self.proc.stdout:
                    if not line:
                        break
                    self.after(0, self._log, line)
        finally:
            rc = None
            try:
                if self.proc:
                    rc = self.proc.wait(timeout=1)
            except Exception:
                try:
                    rc = self.proc.poll()
                except Exception:
                    rc = None

            self.after(0, self._log, f"[ui] server exited rc={rc}\n")
            self.after(0, self.btn_start.configure, {"state": "normal"})
            self.after(0, self.btn_stop.configure, {"state": "disabled"})
            self.after(0, self.btn_restart.configure, {"state": "disabled"})

            # crash guard + auto-restart (NEW)
            if AUTO_RESTART_ON_CRASH and rc not in (0, None):
                now = self._now_s()
                self._crash_times.append(now)

                if not self._crash_window_allows_restart():
                    self.after(0, self._log, "[ui] crash-window guard tripped; auto-restart disabled\n")
                    return

                self.crash_count += 1
                if self.crash_count <= MAX_CRASH_RESTARTS:
                    self._cooldown_until = self._now_s() + RESTART_COOLDOWN_S
                    self.after(0, self._log, f"[ui] auto-restart attempt {self.crash_count} in {AUTO_RESTART_DELAY_MS}ms\n")
                    self.after(AUTO_RESTART_DELAY_MS, self.start_server)
                else:
                    self.after(0, self._log, "[ui] max crash restarts reached\n")
            else:
                self.crash_count = 0
                self._crash_times = []

    def on_close(self):
        try:
            self.stop_server()
        finally:
            self.after(250, self.destroy)

if __name__ == "__main__":
    App().mainloop()
