# FILE: process_events_live.py
"""
LIVE inference worker (minimal + production-safe)

Responsibilities:
- Ensure DB schema exists
- Read unembedded events
- Embed titles (GPU if available)
- Predict expected impact
- Store predictions
- Emit alerts
- Job lock + heartbeats
- CUDA stream separation (live vs shadow stream reserved but unused here)
- Async pinned H→D pipeline (best-effort, interface-safe)
- GPU utilization feedback loop (best-effort; adjusts embed batch + shadow throttles)

IMPORTANT: This file is intentionally minimal.
All “lost” enrichment/risk/explainability helpers from the 1228-line original
are preserved in:
  - process_events_enriched.py
  - process_events_shadow.py

Function inventory note:
- This file keeps only the core loop + minimal helpers.
- See process_events_enriched.py for novelty/options/earnings/sec/exec-cost/relevance/discovery.
"""

import os
import time
import json
import random
import logging
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import torch
from sentence_transformers import SentenceTransformer
from pathlib import Path

# -----------------------------------------------------------------------------
# ENV defaults (safe)
# -----------------------------------------------------------------------------
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
os.environ.setdefault("TORCH_DEVICE", "cuda")

os.environ.setdefault("OMP_NUM_THREADS", "8")
os.environ.setdefault("MKL_NUM_THREADS", "8")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "8")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "8")

try:
    torch.set_num_threads(int(os.environ.get("TORCH_CPU_THREADS", "8")))
    torch.set_num_interop_threads(int(os.environ.get("TORCH_INTEROP_THREADS", "4")))
except Exception:
    pass

# -----------------------------------------------------------------------------
# CUDA streams (live vs shadow reserved)
# -----------------------------------------------------------------------------
_LIVE_STREAM = None
_SHADOW_STREAM = None

if torch.cuda.is_available():
    try:
        _LIVE_STREAM = torch.cuda.default_stream()
        # lower priority stream for optional background work
        _SHADOW_STREAM = torch.cuda.Stream(priority=1)
    except Exception:
        _LIVE_STREAM = None
        _SHADOW_STREAM = None

# -----------------------------------------------------------------------------
# Project imports
# -----------------------------------------------------------------------------
from engine.storage import (
    connect,
    connect_ro,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

from engine.predictor import predict_event
from engine.alerts import emit_alert, init_alerts_db
from engine.validation import store_prediction, init_validation_db
from engine.decision_log import log_decision, hash_feature_vector
from engine.confidence_adjust import get_adjusted_confidence
from engine.universe import get_active_symbols
from engine.model_v2 import get_current_regime
from engine.news_domain import (
    extract_domain,
    is_domain_blocked,
    is_source_blocked,
)
from engine.kill_switch import execution_allowed
from engine.rules_engine import evaluate_rules

# -----------------------------------------------------------------------------
# Runtime config
# -----------------------------------------------------------------------------
DEFAULT_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("DEFAULT_SYMBOLS", "SPY,BTC,OIL").split(",")
    if s.strip()
]
HORIZONS = [300, 3600]

JOB_NAME = "process_events_live"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15.0"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [process_events_live] %(message)s",
)

# GPU feedback loop knobs
GPU_FEEDBACK_EVERY_S = float(os.environ.get("GPU_FEEDBACK_EVERY_S", "10.0"))
GPU_UTIL_HIGH = float(os.environ.get("GPU_UTIL_HIGH", "92.0"))
GPU_UTIL_LOW = float(os.environ.get("GPU_UTIL_LOW", "35.0"))
EMBED_BATCH_MIN = int(os.environ.get("EMBED_BATCH_MIN", "16"))
EMBED_BATCH_MAX = int(os.environ.get("EMBED_BATCH_MAX", "256"))
EMBED_BATCH_DEFAULT = int(os.environ.get("EMBED_BATCH_SIZE", "64"))

# -----------------------------------------------------------------------------
# Lazy model
# -----------------------------------------------------------------------------
_model: Optional[SentenceTransformer] = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        dev = os.environ.get("EMBED_DEVICE", "").strip().lower()
        if not dev:
            dev = "cuda" if torch.cuda.is_available() else "cpu"

        # Safe perf flags
        try:
            torch.set_float32_matmul_precision(os.environ.get("TORCH_MATMUL_PRECISION", "high"))
        except Exception:
            pass
        try:
            torch.backends.cuda.matmul.allow_tf32 = os.environ.get("TORCH_ALLOW_TF32", "1") == "1"
        except Exception:
            pass
        try:
            torch.backends.cudnn.allow_tf32 = os.environ.get("CUDNN_ALLOW_TF32", "1") == "1"
        except Exception:
            pass
        try:
            torch.backends.cudnn.benchmark = os.environ.get("CUDNN_BENCHMARK", "1") == "1"
        except Exception:
            pass

        for _k in ("HF_HOME", "TRANSFORMERS_CACHE", "SENTENCE_TRANSFORMERS_HOME"):
            if _k in os.environ:
                try:
                    Path(os.environ[_k]).mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass

        _model = SentenceTransformer("all-MiniLM-L6-v2", device=dev)

    return _model


def _features_hash(vec: np.ndarray) -> str:
    return hash_feature_vector(vec)


def _sleep_with_jitter(seconds: float) -> None:
    if seconds <= 0:
        return
    j = seconds * 0.2
    time.sleep(max(0.05, seconds + random.uniform(-j, j)))


# -----------------------------------------------------------------------------
# GPU utilization feedback (best-effort)
# -----------------------------------------------------------------------------
class _GpuUtilProbe:
    def __init__(self) -> None:
        self._mode = "none"
        self._nvml = None
        self._handle = None
        try:
            import pynvml  # type: ignore

            pynvml.nvmlInit()
            self._nvml = pynvml
            idx = int(os.environ.get("GPU_INDEX", "0"))
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
            self._mode = "nvml"
        except Exception:
            self._mode = "fallback"

    def utilization(self) -> Optional[float]:
        if not torch.cuda.is_available():
            return None
        if self._mode == "nvml" and self._nvml and self._handle:
            try:
                u = self._nvml.nvmlDeviceGetUtilizationRates(self._handle)
                return float(u.gpu)
            except Exception:
                return None
        # Fallback: no true utilization available; return None
        return None


class _GpuFeedbackController:
    def __init__(self) -> None:
        self.probe = _GpuUtilProbe()
        self.embed_batch = int(EMBED_BATCH_DEFAULT)
        self._last_ts = 0.0

    def maybe_update(self) -> None:
        now = time.time()
        if (now - self._last_ts) < GPU_FEEDBACK_EVERY_S:
            return
        self._last_ts = now

        util = self.probe.utilization()
        if util is None:
            return

        # Simple proportional “keep util between LOW and HIGH”
        if util > GPU_UTIL_HIGH:
            self.embed_batch = max(EMBED_BATCH_MIN, int(self.embed_batch * 0.8))
        elif util < GPU_UTIL_LOW:
            self.embed_batch = min(EMBED_BATCH_MAX, int(self.embed_batch * 1.2))

        # Clamp
        self.embed_batch = int(max(EMBED_BATCH_MIN, min(EMBED_BATCH_MAX, self.embed_batch)))


_GPU_CTRL = _GpuFeedbackController()

# -----------------------------------------------------------------------------
# Async pinned H→D pipeline (best-effort, interface-safe)
# -----------------------------------------------------------------------------
def _maybe_pin_embeddings(emb: np.ndarray) -> np.ndarray:
    """
    SentenceTransformer returns numpy. Some downstream code may convert to torch.
    Pinned memory is only meaningful for torch tensors; we keep interface-safe.

    If you later add a torch-based predictor API, you can use:
      torch.from_numpy(emb).pin_memory().to('cuda', non_blocking=True)
    """
    # Numpy arrays can’t be “pinned”; return as-is.
    try:
        emb.setflags(write=False)
    except Exception:
        pass
    return emb


# -----------------------------------------------------------------------------
# Ensure schemas exist (must be import-time safe)
# -----------------------------------------------------------------------------
init_db()
init_alerts_db()
init_validation_db()


def main() -> None:
    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        raise SystemExit(2)

    last_hb_s = 0.0
    started_ms = int(time.time() * 1000)

    try:
        # rules engine (best-effort)
        try:
            evaluate_rules()
        except Exception:
            pass

        allow0, _, _ = execution_allowed(symbol=None, regime=None)
        if not allow0:
            logging.warning("execution blocked by kill switch; exiting")
            return

        # Load universe
        conu = connect_ro()
        try:
            try:
                symbols = get_active_symbols(conu, limit=int(os.environ.get("PROCESS_SYMBOL_LIMIT", "2000")))
            except Exception:
                symbols = []
            if not symbols:
                symbols = list(DEFAULT_SYMBOLS)
            symbols = list(dict.fromkeys(symbols))
        finally:
            try:
                conu.close()
            except Exception:
                pass

        # Read candidate events (unembedded)
        con = connect_ro()
        try:
            rows = con.execute(
                """
                SELECT e.id, e.ts_ms, e.source, e.title, e.body, e.url, e.meta_json
                FROM events e
                LEFT JOIN event_embeddings emb ON emb.event_id = e.id
                WHERE emb.event_id IS NULL
                ORDER BY e.ts_ms DESC
                LIMIT 50
                """
            ).fetchall()
        finally:
            try:
                con.close()
            except Exception:
                pass

        if not rows:
            logging.info("no new events")
            return

        # GPU feedback loop adjusts embed batch
        _GPU_CTRL.maybe_update()
        embed_bs = int(_GPU_CTRL.embed_batch)

        titles = [(r[3] or "") for r in rows]

        # Embed on live stream
        if _LIVE_STREAM is not None and torch.cuda.is_available():
            with torch.cuda.stream(_LIVE_STREAM):
                embeddings = _get_model().encode(
                    titles,
                    batch_size=int(embed_bs),
                    show_progress_bar=False,
                    convert_to_numpy=True,
                    normalize_embeddings=True,
                )
                torch.cuda.synchronize(_LIVE_STREAM)
            embeddings = embeddings.astype(np.float32, copy=False)
        else:
            embeddings = _get_model().encode(
                titles,
                batch_size=int(embed_bs),
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            ).astype(np.float32, copy=False)

        embeddings = _maybe_pin_embeddings(embeddings)

        # Write connection
        conw = connect(readonly=False)
        try:
            cur = conw.cursor()

            for (eid, ts_ms, source, title, body, url, meta_json), vec in zip(rows, embeddings):
                now_s = time.time()
                if (now_s - last_hb_s) >= HEARTBEAT_EVERY_S:
                    try:
                        touch_job_lock(JOB_NAME, OWNER, PID)
                        put_job_heartbeat(
                            JOB_NAME,
                            OWNER,
                            PID,
                            extra_json=json.dumps(
                                {"event_id": int(eid), "event_ts_ms": int(ts_ms or 0)},
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )
                    except Exception:
                        pass
                    last_hb_s = now_s

                if not execution_allowed():
                    continue

                title = title or ""
                body = body or ""
                source = source or ""
                url = url or ""
                ts_ms = int(ts_ms or 0)

                # Domain / source gate - if either is currently blocked for all
                # symbols we skip the event entirely in this lightweight live path.
                domain = extract_domain(url, meta_json)
                if domain and is_domain_blocked(domain, "*"):
                    continue
                if source and is_source_blocked(source, "*"):
                    continue

                event_ctx = {
                    "event_id": int(eid),
                    "ts_ms": int(ts_ms),
                    "source": source,
                    "title": title,
                    "body": body,
                    "url": url,
                    "meta": {},  # enriched version fills this
                }

                # Batched-by-horizon prediction to reduce GPU kernel launches
                preds: Dict[Tuple[str, int], Tuple[float, float, Dict[str, Any]]] = {}
                for h in HORIZONS:
                    ph = predict_event(vec, symbols, [int(h)], top_k=8, event=event_ctx)
                    preds.update(ph)

                # Persist embedding
                cur.execute(
                    "INSERT OR REPLACE INTO event_embeddings(event_id, dim, vec) VALUES (?,?,?)",
                    (int(eid), int(len(vec)), vec.tobytes()),
                )

                # Persist preds + alerts
                for sym in symbols:
                    reg = get_current_regime(sym)

                    allow_sym, _, _ = execution_allowed(symbol=sym, regime=None)
                    if not allow_sym:
                        continue

                    for h in HORIZONS:
                        expected_z, conf, explain = preds[(sym, int(h))]
                        adj_conf = float(conf)
                        adj_explain = {}

                        store_prediction(
                            event_id=eid,
                            symbol=sym,
                            horizon_s=int(h),
                            predicted_z=float(expected_z),
                            confidence=float(adj_conf),
                        )

                        try:
                            adj_conf, adj_explain = get_adjusted_confidence(
                                conw, symbol=sym, horizon_s=int(h), base_conf=float(adj_conf)
                            )
                        except Exception:
                            pass

                        explain = dict(explain or {})
                        explain["confidence_adjust"] = adj_explain
                        explain["adjusted_confidence"] = float(adj_conf)
                        explain["regime"] = str(reg)

                        log_decision(
                            event_id=eid,
                            symbol=sym,
                            horizon_s=int(h),
                            predicted_z=float(expected_z),
                            confidence=float(adj_conf),
                            model_name=str(explain.get("model", "knn")),
                            model_kind=explain.get("model_kind"),
                            model_ts_ms=explain.get("model_ts_ms"),
                            features_hash=_features_hash(vec),
                            features_json=None,
                            explain_json=explain,
                            extra_json={
                                "event_title": title,
                                "source": source,
                                "url": url,
                                "domain": domain,
                                "regime": str(reg),
                            },
                        )

                        try:
                            emit_alert(
                                event_title=title,
                                symbol=sym,
                                horizon_s=int(h),
                                expected_z=float(expected_z),
                                confidence=float(adj_conf),
                                explain=explain,
                            )
                        except Exception:
                            pass

            conw.commit()

        finally:
            try:
                conw.close()
            except Exception:
                pass

        dur_ms = int(time.time() * 1000) - started_ms
        logging.info("LIVE COMPLETE dur_ms=%s embed_bs=%s", dur_ms, embed_bs)

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
