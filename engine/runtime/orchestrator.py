# engine/runtime/orchestrator.py
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
import threading
from typing import Dict

from engine.runtime.job_registry import PIPELINE_ORDER
from engine.dev_core.storage import connect as _db_connect


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

    # ---------------------------------------------------
    # Helpers
    # ---------------------------------------------------

    def _is_job_running(self, name: str) -> bool:
        j = self.JOBS.get(name)
        if not j:
            return False
        p = j.proc
        if not p:
            return False
        try:
            return p.poll() is None
        except Exception:
            return False

    # ---------------------------------------------------
    # PIPELINE
    # ---------------------------------------------------

    def run_pipeline(self, include_execution: bool | None = None) -> Dict:
        """
        Runs pipeline jobs in PIPELINE_ORDER.
        include_execution:
          - None  => uses AUTO_PIPELINE_INCLUDE_EXECUTION (default behavior)
          - True  => include portfolio_rebalance + broker_apply_orders
          - False => skip execution legs
        """
        if not self._acquire_lock("pipeline", ttl_ms=20 * 60 * 1000):
            return {"ok": False, "error": "pipeline locked (already running?)"}

        include_exec = (
            bool(self.AUTO_PIPELINE_INCLUDE_EXECUTION)
            if include_execution is None
            else bool(include_execution)
        )

        try:
            if not self._is_job_running("poll_prices"):
                return {"ok": False, "error": "poll_prices must be running before pipeline"}

            for name in PIPELINE_ORDER:

                if name in ("portfolio_rebalance", "broker_apply_orders") and not include_exec:
                    continue

                job = self.JOBS.get(name)
                if not job or job.mode == "daemon":
                    continue

                if name == "broker_apply_orders":
                    pr = self.JOBS.get("portfolio_rebalance")
                    if not pr or not pr.exited_at_ms:
                        return {"ok": False, "error": "broker_apply_orders requires portfolio_rebalance first"}

                res = self.JOBS.start(name)
                if not res.get("ok"):
                    return {"ok": False, "error": f"{name}: {res.get('error')}"}

                while True:
                    time.sleep(0.2)
                    if not job.proc:
                        break
                    if job.proc.poll() is not None:
                        if job.exit_code not in (0, None):
                            return {"ok": False, "error": f"{name} exited rc={job.exit_code}"}
                        break

            return {"ok": True}

        finally:
            self._release_lock("pipeline")

    # ---------------------------------------------------
    # AUTO PIPELINE
    # ---------------------------------------------------

    def auto_pipeline_loop(self):
        time.sleep(max(0.0, float(self.AUTO_PIPELINE_START_DELAY_S)))

        while True:
            try:
                if not self._is_job_running("poll_prices"):
                    res = self.JOBS.start("poll_prices")
                    if self.AUTO_PIPELINE_LOG:
                        print("[auto_pipeline] poll_prices start:", res)

                res = self.run_pipeline()
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
            if not res.get("ok"):
                return res

            job = self.JOBS.get("train_and_eval_challenger")

            while True:
                time.sleep(0.25)
                if not job or not job.proc:
                    break
                if job.proc.poll() is not None:
                    if job.exit_code not in (0, None):
                        return {"ok": False, "error": f"train_and_eval_challenger exited rc={job.exit_code}"}
                    break

            return {"ok": True}
        finally:
            self._release_lock("challenger")

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

                        # if it's a one-shot job, wait for completion so lock reflects actual run
                        job = self.JOBS.get("train_size_policy")
                        if job and getattr(job, "mode", "") != "daemon":
                            while True:
                                time.sleep(0.25)
                                if not job.proc:
                                    break
                                if job.proc.poll() is not None:
                                    break
                    finally:
                        self._release_lock("train_size_policy")

            except Exception as e:
                if self.AUTO_SIZE_POLICY_LOG:
                    print("[auto_size_policy] ERROR:", str(e))

            time.sleep(max(300.0, float(self.AUTO_SIZE_POLICY_INTERVAL_S)))
