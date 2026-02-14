import subprocess
import threading
import time
import os
from typing import Dict, Optional


class JobSpec:
    def __init__(self, name: str, script: str, daemon: bool = False):
        self.name = name
        self.script = script
        self.daemon = daemon


class JobState:
    def __init__(self, spec: JobSpec):
        self.spec = spec
        self.process: Optional[subprocess.Popen] = None
        self.last_heartbeat_ts = 0
        self.last_start_ts = 0
        self.last_exit_code: Optional[int] = None
        self.restart_count = 0


class RuntimeSupervisor:
    def __init__(self):
        self._jobs: Dict[str, JobState] = {}
        self._lock = threading.Lock()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True
        )
        self._monitor_thread.start()

    # -----------------------------------
    # Registration
    # -----------------------------------

    def register_job(self, name: str, script: str, daemon: bool = False):
        with self._lock:
            if name in self._jobs:
                raise ValueError(f"Job already registered: {name}")
            spec = JobSpec(name=name, script=script, daemon=daemon)
            self._jobs[name] = JobState(spec)

    # -----------------------------------
    # Control
    # -----------------------------------

    def start(self, name: str):
        with self._lock:
            state = self._require(name)

            if state.process and state.process.poll() is None:
                return  # already running

            state.process = subprocess.Popen(
                ["python", state.spec.script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            state.last_start_ts = time.time()
            state.last_exit_code = None

    def stop(self, name: str):
        with self._lock:
            state = self._require(name)
            if state.process and state.process.poll() is None:
                state.process.terminate()
                state.process.wait(timeout=5)
            state.process = None

    def restart(self, name: str):
        self.stop(name)
        self.start(name)

    # -----------------------------------
    # Status
    # -----------------------------------

    def status(self):
        out = {}
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
                }
        return out

    # -----------------------------------
    # Heartbeat
    # -----------------------------------

    def heartbeat(self, name: str):
        with self._lock:
            state = self._require(name)
            state.last_heartbeat_ts = time.time()

    # -----------------------------------
    # Monitor Loop
    # -----------------------------------

    def _monitor_loop(self):
        while True:
            time.sleep(2)

            with self._lock:
                for name, state in self._jobs.items():
                    if not state.process:
                        continue

                    exit_code = state.process.poll()

                    # process exited
                    if exit_code is not None:
                        state.last_exit_code = exit_code
                        state.process = None

                        # restart daemons automatically
                        if state.spec.daemon:
                            state.restart_count += 1
                            state.process = subprocess.Popen(
                                ["python", state.spec.script],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            state.last_start_ts = time.time()

    def _require(self, name: str) -> JobState:
        if name not in self._jobs:
            raise ValueError(f"Unknown job: {name}")
        return self._jobs[name]
