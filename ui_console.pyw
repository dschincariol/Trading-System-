"""ui_console.pyw

Double-click launcher for the local dashboard server (Windows).

- Starts/stops dashboard_server.py without opening a command prompt.
- Shows server stdout in a small GUI console.
- Opens the browser to /ui/dashboard.html
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


PORT = 8000
URL = f"http://localhost:{PORT}/ui/dashboard.html"
STATUS_URL = f"http://localhost:{PORT}/api/server/status"
SHUTDOWN_URL = f"http://localhost:{PORT}/api/server/shutdown"

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

    # 1) Create core DB tables (dev_core/storage.py)
    ok = _run_and_stream(
        app,
        [py, "-u", "-c", "from engine.dev_core.storage import init_db; init_db(); print('[startup] init_db ok')"],
        label="[startup]"
    )
    if not ok:
        return False

    # 2) Create module-owned schemas that preflight expects to exist
    ok = _run_and_stream(
        app,
        [py, "-u", "-c",
         "from engine.dev_core.portfolio import init_portfolio_db; "
         "from engine.dev_core.broker_sim import init_broker_db; "
         "from engine.dev_core.alerts import init_alerts_db; "
         "from engine.dev_core.validation import init_validation_db; "
         "from engine.dev_core.model_v2 import init_model_db; "
         "init_portfolio_db(); init_broker_db(); init_alerts_db(); init_validation_db(); init_model_db(); "
         "print('[startup] module db init ok')"],
        label="[startup]"
    )
    if not ok:
        return False

    # 3) Ensure backtest output tables exist (created from portfolio_backtest.SCHEMA)
    ok = _run_and_stream(
        app,
        [py, "-u", "-c",
         "import portfolio_backtest as p; "
         "from engine.dev_core.storage import connect; "
         "con=connect(); con.executescript(p.SCHEMA); con.commit(); con.close(); "
         "print('[startup] portfolio_backtest schema ok')"],
        label="[startup]"
    )
    if not ok:
        return False

    # 4) Optional: bootstrap exec labels + size policy now (dashboard will also attempt on its own)
    _run_and_stream(app, [py, "-u", "compute_exec_labels.py"], label="[startup]")
    _run_and_stream(app, [py, "-u", "train_size_policy.py"], label="[startup]")

    return True

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Market Impact — Dashboard Console")
        self.geometry("900x560")

        self.proc = None
        self._pump_thread = None

        top = tk.Frame(self)
        top.pack(fill="x", padx=10, pady=10)

        self.lbl = tk.Label(top, text=URL, anchor="w")
        self.lbl.pack(side="left", fill="x", expand=True)

        self.status_pill = tk.Label(top, text="OFFLINE", padx=10, pady=2)
        self.status_pill.pack(side="left", padx=(10, 0))

        self.uptime_lbl = tk.Label(top, text="uptime: —", anchor="w")
        self.uptime_lbl.pack(side="left", padx=(10, 0))

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

        self.txt = tk.Text(self, wrap="none")
        self.txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._log("Ready. Click 'Start Server'.\n")
        self._set_status(False, 0)
        self.after(300, self._poll_status_loop)

    def _log(self, s: str):
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
        # start after a short delay
        self.after(800, self.start_server)

    def _set_status(self, online: bool, uptime_s: int = 0):
        if online:
            self.status_pill.configure(text="ONLINE", bg="#1f6f3b", fg="white")
            self.uptime_lbl.configure(text=f"uptime: {uptime_s}s")
        else:
            self.status_pill.configure(text="OFFLINE", bg="#7a1f1f", fg="white")
            self.uptime_lbl.configure(text="uptime: —")

    def _poll_status_loop(self):
        # runs forever while UI is open
        j = _http_json(STATUS_URL)
        if j and j.get("ok"):
            self._set_status(True, int(j.get("uptime_s") or 0))
        else:
            self._set_status(False, 0)
        self.after(1000, self._poll_status_loop)

    def start_server(self):
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
            # ask the server to shutdown (stops child jobs too)
            j = _http_json(SHUTDOWN_URL)
            if j and j.get("ok"):
                self._log("[ui] graceful shutdown requested\n")

        # fallback: terminate the process if still alive shortly after
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

    def on_close(self):
        try:
            self.stop_server()
        finally:
            self.after(250, self.destroy)


if __name__ == "__main__":
    App().mainloop()
