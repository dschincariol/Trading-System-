# FILE: process_events_shadow.py
"""
SHADOW worker (research-only / background heavy compute)

Responsibilities:
- Do NOT emit live alerts (default)
- Run expensive shadow predictors / evaluation
- Optional clustering, regime experiments, backtests, temporal models
- Uses dedicated low-priority CUDA stream (shadow)

This file is where you put:
- temporal shadow prediction
- experimental models
- training jobs that must not starve live inference

IMPORTANT:
- If you want training here, keep it on _SHADOW_STREAM
- Consider also lowering CPU priority / using separate machine for training
"""

import os
import time
import json
import logging
from typing import Any, Dict, List, Optional

import numpy as np
import torch

# -----------------------------------------------------------------------------
# ENV defaults
# -----------------------------------------------------------------------------
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
os.environ.setdefault("TORCH_DEVICE", "cuda")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [process_events_shadow] %(message)s",
)

# -----------------------------------------------------------------------------
# CUDA streams (shadow-focused)
# -----------------------------------------------------------------------------
_LIVE_STREAM = None
_SHADOW_STREAM = None
if torch.cuda.is_available():
    try:
        _LIVE_STREAM = torch.cuda.default_stream()
        _SHADOW_STREAM = torch.cuda.Stream(priority=1)
    except Exception:
        _LIVE_STREAM = None
        _SHADOW_STREAM = None

# -----------------------------------------------------------------------------
# Project imports
# -----------------------------------------------------------------------------
from engine.dev_core.storage import (
    connect,
    connect_ro,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

from engine.dev_core.universe import get_active_symbols
from engine.dev_core.rules_engine import evaluate_rules
from engine.dev_core.kill_switch import execution_allowed

# Optional heavy subsystems
try:
    from engine.dev_core.temporal_predictor import predict_temporal_shadow_for_event
except Exception:
    predict_temporal_shadow_for_event = None

try:
    from engine.dev_core.clustering import assign_cluster
except Exception:
    assign_cluster = None

# -----------------------------------------------------------------------------
# Runtime config
# -----------------------------------------------------------------------------
JOB_NAME = "process_events_shadow"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15.0"))

DEFAULT_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("DEFAULT_SYMBOLS", "SPY,BTC,OIL").split(",")
    if s.strip()
]
HORIZONS = [300, 3600]

# Shadow behavior toggles
SHADOW_EMIT_ALERTS = os.environ.get("SHADOW_EMIT_ALERTS", "0") == "1"
MAX_EVENTS_PER_PASS = int(os.environ.get("SHADOW_MAX_EVENTS_PER_PASS", "25"))

# -----------------------------------------------------------------------------
# Ensure schemas exist
# -----------------------------------------------------------------------------
init_db()


def main() -> None:
    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        raise SystemExit(2)

    last_hb_s = 0.0

    try:
        try:
            evaluate_rules()
        except Exception:
            pass

        # Shadow respects kill-switch too (don’t waste cycles if disabled)
        allow0, _, _ = execution_allowed(symbol=None, regime=None)
        if not allow0:
            logging.warning("shadow blocked by kill switch; exiting")
            return

        # Symbols
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

        # Pull recent events that already have embeddings (shadow wants embedded)
        con = connect_ro()
        try:
            rows = con.execute(
                """
                SELECT e.id, e.ts_ms, e.title, emb.dim, emb.vec
                FROM events e
                JOIN event_embeddings emb ON emb.event_id = e.id
                ORDER BY e.ts_ms DESC
                LIMIT ?
                """,
                (int(MAX_EVENTS_PER_PASS),),
            ).fetchall()
        finally:
            try:
                con.close()
            except Exception:
                pass

        if not rows:
            logging.info("no embedded events for shadow")
            return

        conw = connect(readonly=False)
        try:
            for (eid, ts_ms, title, dim, blob) in rows:
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

                if not predict_temporal_shadow_for_event and not assign_cluster:
                    continue

                vec = None
                try:
                    d = int(dim or 0)
                    if d > 0 and blob:
                        a = np.frombuffer(blob, dtype=np.float32)
                        if a.size == d:
                            vec = a
                except Exception:
                    vec = None

                # Optional clustering (shadow)
                if assign_cluster and vec is not None:
                    try:
                        if _SHADOW_STREAM is not None and torch.cuda.is_available():
                            with torch.cuda.stream(_SHADOW_STREAM):
                                _ = assign_cluster(event_id=int(eid), ts_ms=int(ts_ms or 0), title=(title or ""), vec=vec)
                        else:
                            _ = assign_cluster(event_id=int(eid), ts_ms=int(ts_ms or 0), title=(title or ""), vec=vec)
                    except Exception:
                        pass

                # Temporal shadow predictor (shadow stream)
                if predict_temporal_shadow_for_event:
                    try:
                        if _SHADOW_STREAM is not None and torch.cuda.is_available():
                            with torch.cuda.stream(_SHADOW_STREAM):
                                _ = predict_temporal_shadow_for_event(
                                    conw,
                                    event_id=int(eid),
                                    ts_ms=int(ts_ms or 0),
                                    symbols=symbols,
                                    horizons=HORIZONS,
                                )
                        else:
                            _ = predict_temporal_shadow_for_event(
                                conw,
                                event_id=int(eid),
                                ts_ms=int(ts_ms or 0),
                                symbols=symbols,
                                horizons=HORIZONS,
                            )
                    except Exception:
                        pass

            conw.commit()
        finally:
            try:
                conw.close()
            except Exception:
                pass

        logging.info("SHADOW COMPLETE n_events=%s", len(rows))

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
