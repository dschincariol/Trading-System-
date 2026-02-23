"""
Runtime Orchestrator

Extracted from dashboard_server.py

Owns:
- run_pipeline
- auto pipeline loop
- auto challenger loop
- auto size policy loop

No HTTP logic.
No API logic.
Pure runtime orchestration.
"""

import time
from typing import Dict, Optional, Callable
from engine.runtime.job_registry import PIPELINE_ORDER
from engine.runtime.storage import connect as _db_connect
from engine.runtime.gates import execution_gate_snapshot, is_execution_job

class RuntimeOrchestrator:
    def __init__(
        self,
        jobs,
        acquire_lock,
        release_lock,
        auto_pipeline_include_execution: bool,
        auto_pipeline_log: bool,
        auto_pipeline_interval_s: float,
        auto_pipeline_start_delay_s: float,
        auto_challenger_log: bool,
        auto_challenger_interval_s: float,
        auto_challenger_start_delay_s: float,
        auto_challenger_min_drift: float,
        auto_size_policy_log: bool,
        auto_size_policy_interval_s: float,
        auto_size_policy_start_delay_s: float,
        # injected (keeps orchestrator API-free)
        get_kill_switches: Optional[Callable[[], dict]] = None,
        get_execution_mode: Optional[Callable[[], dict]] = None,
    ):
        self.JOBS = jobs
        self._acquire_lock = acquire_lock
        self._release_lock = release_lock

        self.AUTO_PIPELINE_INCLUDE_EXECUTION = auto_pipeline_include_execution
        self.AUTO_PIPELINE_LOG = auto_pipeline_log
        self.AUTO_PIPELINE_INTERVAL_S = auto_pipeline_interval_s
        self.AUTO_PIPELINE_START_DELAY_S = auto_pipeline_start_delay_s

        self.AUTO_CHALLENGER_LOG = auto_challenger_log
        self.AUTO_CHALLENGER_INTERVAL_S = auto_challenger_interval_s
        self.AUTO_CHALLENGER_START_DELAY_S = auto_challenger_start_delay_s
        self.AUTO_CHALLENGER_MIN_DRIFT = auto_challenger_min_drift

        self.AUTO_SIZE_POLICY_LOG = auto_size_policy_log
        self.AUTO_SIZE_POLICY_INTERVAL_S = auto_size_policy_interval_s
        self.AUTO_SIZE_POLICY_START_DELAY_S = auto_size_policy_start_delay_s

        self._get_kill_switches = get_kill_switches or (lambda: {})
        self._get_execution_mode = get_execution_mode or (lambda: {})

    # ---------------------------------------------------
    # Helpers
    # ---------------------------------------------------

    def _is_job_running(self, name: str) -> bool:
        j = self.JOBS.get(name)
        if not j:
            return False
        p = getattr(j, "proc", None)
        if not p:
            return False
        try:
            return p.poll() is None
        except Exception:
            return False

    def _wait_job_exit(self, name: str, poll_s: float = 0.25) -> Dict:
        """
        Wait for a one-shot job to exit.
        Daemons are not waited on.
        """
        job = self.JOBS.get(name)
        if not job:
            return {"ok": False, "error": f"job_missing:{name}"}

        # best-effort: if job exposes mode, don't wait on daemon
        try:
            if str(getattr(job, "mode", "") or "").lower() == "daemon":
                return {"ok": True, "daemon": True}
        except Exception:
            pass

        while True:
            time.sleep(poll_s)
            job = self.JOBS.get(name)
            if not job:
                break
            p = getattr(job, "proc", None)
            if not p:
                break
            try:
                rc = p.poll()
            except Exception:
                rc = None
            if rc is not None:
                try:
                    exit_code = getattr(job, "exit_code", rc)
                except Exception:
                    exit_code = rc
                if exit_code not in (0, None):
                    return {"ok": False, "error": f"{name} exited rc={exit_code}"}
                return {"ok": True, "rc": exit_code}

    # ---------------------------------------------------
    # PIPELINE
    # ---------------------------------------------------

    def run_pipeline(self, include_execution: bool = False):
        """
        Runs jobs in PIPELINE_ORDER.
        include_execution=False will skip execution jobs.

        HARD EXECUTION GATING (fail-closed):
          - if include_execution=True and any execution job would run,
            require execution_gate_snapshot().ok == True
        """
        # Fail-closed gate: don't even attempt execution jobs unless LIVE+armed
        if include_execution:
            try:
                wants_exec = any(is_execution_job(n) for n in (PIPELINE_ORDER or []))
            except Exception:
                wants_exec = False

            if wants_exec:
                snap = execution_gate_snapshot()
                if not snap.get("ok"):
                    return {
                        "ok": False,
                        "error": "execution_gate_blocked",
                        "gate": snap,
                        "results": [],
                    }

        results = []
        for name in PIPELINE_ORDER:
            if (not include_execution) and is_execution_job(name):
                continue

    # ---------------------------------------------------
    # AUTO PIPELINE
    # ---------------------------------------------------

    def auto_pipeline_loop(self):
        time.sleep(max(0.0, float(self.AUTO_PIPELINE_START_DELAY_S)))

        while True:
            try:
                # safety: keep prices flowing
                if not self._is_job_running("poll_prices"):
                    res = self.JOBS.start("poll_prices")
                    if self.AUTO_PIPELINE_LOG:
                        print("[auto_pipeline] poll_prices start:", res)

                res = self.run_pipeline(include_execution=bool(self.AUTO_PIPELINE_INCLUDE_EXECUTION))
                if self.AUTO_PIPELINE_LOG:
                    print("[auto_pipeline] run_pipeline:", res)

            except Exception as e:
                if self.AUTO_PIPELINE_LOG:
                    print("[auto_pipeline] ERROR:", str(e))

            time.sleep(max(5.0, float(self.AUTO_PIPELINE_INTERVAL_S)))

    # ---------------------------------------------------
    # CHALLENGER
    # ---------------------------------------------------

    def _max_drift_ratio(self) -> float:
        con = _db_connect()
        try:
            try:
                r = con.execute("SELECT MAX(drift_ratio) FROM model_drift").fetchone()
                return float(r[0] or 0.0) if r else 0.0
            except Exception:
                return 0.0
        finally:
            con.close()

    def _run_challenger_job_wait(self) -> Dict:
        if not self._acquire_lock("challenger", ttl_ms=30 * 60 * 1000):
            return {"ok": False, "error": "challenger locked (already running?)"}

        try:
            res = self.JOBS.start("train_and_eval_challenger")
            if not isinstance(res, dict) or not res.get("ok"):
                return res if isinstance(res, dict) else {"ok": False, "error": "start_failed"}

            # wait if one-shot
            wait_res = self._wait_job_exit("train_and_eval_challenger")
            if not wait_res.get("ok"):
                return wait_res

            return {"ok": True}
        finally:
            try:
                self._release_lock("challenger")
            except Exception:
                pass

    def auto_challenger_loop(self):
        time.sleep(max(0.0, float(self.AUTO_CHALLENGER_START_DELAY_S)))

        while True:
            try:
                if self.AUTO_CHALLENGER_MIN_DRIFT > 0.0:
                    md = self._max_drift_ratio()
                    if md < self.AUTO_CHALLENGER_MIN_DRIFT:
                        if self.AUTO_CHALLENGER_LOG:
                            print(f"[auto_challenger] skip drift_gate max_drift={md:.3f}")
                    else:
                        out = self._run_challenger_job_wait()
                        if self.AUTO_CHALLENGER_LOG:
                            print("[auto_challenger] result:", out)
                else:
                    out = self._run_challenger_job_wait()
                    if self.AUTO_CHALLENGER_LOG:
                        print("[auto_challenger] result:", out)

            except Exception as e:
                if self.AUTO_CHALLENGER_LOG:
                    print("[auto_challenger] ERROR:", str(e))

            time.sleep(max(30.0, float(self.AUTO_CHALLENGER_INTERVAL_S)))

    # ---------------------------------------------------
    # SIZE POLICY
    # ---------------------------------------------------

    def auto_size_policy_loop(self):
        time.sleep(max(0.0, float(self.AUTO_SIZE_POLICY_START_DELAY_S)))

        while True:
            try:
                if self.AUTO_SIZE_POLICY_LOG:
                    print("[auto_size_policy] running train_size_policy")

                if not self._acquire_lock("train_size_policy", ttl_ms=30 * 60 * 1000):
                    if self.AUTO_SIZE_POLICY_LOG:
                        print("[auto_size_policy] skip: train_size_policy locked (already running?)")
                else:
                    try:
                        res = self.JOBS.start("train_size_policy")
                        if self.AUTO_SIZE_POLICY_LOG:
                            print("[auto_size_policy] result:", res)

                        wait_res = self._wait_job_exit("train_size_policy")
                        if self.AUTO_SIZE_POLICY_LOG and not wait_res.get("ok"):
                            print("[auto_size_policy] wait ERROR:", wait_res)

                    finally:
                        try:
                            self._release_lock("train_size_policy")
                        except Exception:
                            pass

            except Exception as e:
                if self.AUTO_SIZE_POLICY_LOG:
                    print("[auto_size_policy] ERROR:", str(e))

            time.sleep(max(300.0, float(self.AUTO_SIZE_POLICY_INTERVAL_S)))
