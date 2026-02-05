# dev_core/predictor.py
import math
import os
import time
import json
import logging
from typing import Dict, List, Tuple, Optional

import numpy as np
from sklearn.metrics.pairwise import cosine_similarity

from dev_core.storage import connect
from dev_core.learning import (
    confidence_from_weight,
    confidence_from_n,
    get_global_prior,
    learn_relevance_stats,
)
from dev_core.model_v2 import get_regime_prior, get_spillover_betas

# ------------------------------------------------------------
# Option A: supervised embedding regressor (OPT-IN)
# ------------------------------------------------------------
from dev_core.embed_regressor import predict_with_embed_model
from dev_core.feature_expansion import build_feature_vector

_USE_EMBED_REGRESSOR = os.environ.get("USE_EMBED_REGRESSOR", "0") == "1"
_EMBED_REGRESSOR_CONF_K = float(os.environ.get("EMBED_REGRESSOR_CONF_K", "75.0"))
_EMBED_CONF_CALIB = os.environ.get("EMBED_CONF_CALIB", "1") == "1"

# small cache for calibration curves
_CALIB_CACHE_TTL_S = 60.0
_calib_cache = {
    "ts_s": 0.0,
    "curves": {},  # (horizon_s, model_kind) -> (xs, ys)
}


def _load_embed_conf_calib(con, horizon_s: int, model_kind: str):
    key = (int(horizon_s), str(model_kind))
    now_s = time.time()
    if (now_s - float(_calib_cache["ts_s"])) < float(_CALIB_CACHE_TTL_S) and key in _calib_cache["curves"]:
        return _calib_cache["curves"][key]

    row = con.execute(
        """
        SELECT x_json, y_json
        FROM embed_conf_calib
        WHERE horizon_s=? AND model_kind=?
        """,
        (int(horizon_s), str(model_kind)),
    ).fetchone()
    if not row:
        return None

    try:
        xs = [float(x) for x in json.loads(row[0] or "[]")]
        ys = [float(y) for y in json.loads(row[1] or "[]")]
        if len(xs) < 2 or len(xs) != len(ys):
            return None
    except Exception:
        return None

    _calib_cache["ts_s"] = float(now_s)
    _calib_cache["curves"][key] = (xs, ys)
    return xs, ys


def _apply_calib(conf_raw: float, curve):
    try:
        xs, ys = curve
        x = float(conf_raw)
        # clip
        if x <= xs[0]:
            return float(max(0.0, min(1.0, ys[0])))
        if x >= xs[-1]:
            return float(max(0.0, min(1.0, ys[-1])))
        # linear interp
        return float(max(0.0, min(1.0, float(np.interp(x, np.asarray(xs), np.asarray(ys))))))
    except Exception:
        return float(conf_raw)


HALF_LIFE_DAYS = 7.0
MS_PER_DAY = 24 * 3600 * 1000

MIN_BETA_N = 10

# ------------------------------------------------------------
# Prediction core
# ------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [predictor] %(message)s",
)

# Confidence-collapse guardrails
CONF_COLLAPSE_MIN = float(os.environ.get("CONF_COLLAPSE_MIN", "0.15"))
CONF_COLLAPSE_FRAC = float(os.environ.get("CONF_COLLAPSE_FRAC", "0.7"))

# ------------------------------------------------------------
# Option 4: performance/scalability cache (behavior-preserving)
# ------------------------------------------------------------

_CACHE_TTL_S = 10.0  # short TTL; invalidation also uses label stamp

_cached = {
    "ts_s": 0.0,                # last refresh time (monotonic seconds)
    "stamp": None,              # (label_count, max_created_at_ms)
    "events": None,             # list[(event_id, ts_ms, vec)]
    "labels": None,             # dict[(event_id,symbol,horizon_s)] -> impact_z
    "vecs": None,               # np.ndarray shape [N, D]
    "event_ids": None,          # list[int]
    "event_ts": None,           # list[int]
}

# ------------------------------------------------------------
# Option 5.1: learned relevance → confidence scaling (OPT-IN)
# ------------------------------------------------------------

_USE_LEARNED_REL = os.environ.get("PREDICTOR_USE_LEARNED_RELEVANCE", "0") == "1"
_LEARNED_REL_ABS_Z = float(os.environ.get("PREDICTOR_LEARNED_REL_ABS_Z", "0.5"))
# multiplier = floor + (ceil-floor)*learned_relevance
_LEARNED_REL_CONF_FLOOR = float(os.environ.get("PREDICTOR_LEARNED_REL_CONF_FLOOR", "0.5"))
_LEARNED_REL_CONF_CEIL = float(os.environ.get("PREDICTOR_LEARNED_REL_CONF_CEIL", "1.0"))


def _labels_stamp(con) -> Tuple[int, int]:
    """
    Stamp for invalidation: (count, max_created_at_ms).
    Uses only existing columns.
    """
    try:
        row = con.execute(
            """
            SELECT COUNT(*), MAX(created_at_ms)
            FROM labels
            WHERE impact_z IS NOT NULL
            """
        ).fetchone()
    except Exception:
        return 0, 0

    if not row:
        return 0, 0

    try:
        n = int(row[0] or 0)
    except Exception:
        n = 0

    try:
        mx = int(row[1] or 0)
    except Exception:
        mx = 0

    return n, mx


def load_labeled_event_vectors():
    """
    Returns:
      events: list of (event_id, ts_ms, vector)
      labels: dict[(event_id, symbol, horizon_s)] -> impact_z
    """
    con = connect()
    try:
        use_temporal = os.environ.get("USE_TEMPORAL_EMBED", "0") == "1"
        emb_table = "event_embeddings_seq" if use_temporal else "event_embeddings"

        evs = con.execute(
            f"""
            SELECT e.id, e.ts_ms, emb.vec
            FROM events e
            JOIN {emb_table} emb ON emb.event_id = e.id
            """
        ).fetchall()

        lbls = con.execute(
            """
            SELECT event_id, symbol, horizon_s, impact_z
            FROM labels
            WHERE impact_z IS NOT NULL
            """
        ).fetchall()

        events = []
        for eid, ts_ms, blob in evs:
            vec = np.frombuffer(blob, dtype=np.float32)
            events.append((int(eid), int(ts_ms), vec))

        labels = {}
        for eid, sym, h, z in lbls:
            labels[(int(eid), str(sym), int(h))] = float(z)

        return events, labels
    finally:
        con.close()


def _load_labeled_event_vectors_cached():
    """
    Cached version of load_labeled_event_vectors().
    Builds vec matrix + metadata once.
    """
    now_s = time.monotonic()
    if (
        _cached["events"] is not None
        and _cached["labels"] is not None
        and _cached["vecs"] is not None
        and (now_s - float(_cached["ts_s"] or 0.0)) < _CACHE_TTL_S
    ):
        return _cached["events"], _cached["labels"], _cached["vecs"], _cached["event_ids"], _cached["event_ts"]

    con = connect()
    try:
        stamp = _labels_stamp(con)
        if _cached["stamp"] == stamp and _cached["events"] is not None and _cached["vecs"] is not None:
            _cached["ts_s"] = now_s
            return _cached["events"], _cached["labels"], _cached["vecs"], _cached["event_ids"], _cached["event_ts"]
    finally:
        con.close()

    events, labels = load_labeled_event_vectors()
    if not events:
        _cached.update({
            "ts_s": now_s,
            "stamp": stamp,
            "events": [],
            "labels": labels,
            "vecs": None,
            "event_ids": [],
            "event_ts": [],
        })
        return _cached["events"], _cached["labels"], _cached["vecs"], _cached["event_ids"], _cached["event_ts"]

    try:
        vecs = np.stack([v for _, _, v in events]).astype(np.float32, copy=False)
    except Exception:
        vecs = None

    event_ids = [int(eid) for (eid, _, _) in events]
    event_ts = [int(ts) for (_, ts, _) in events]

    _cached.update({
        "ts_s": now_s,
        "stamp": stamp,
        "events": events,
        "labels": labels,
        "vecs": vecs,
        "event_ids": event_ids,
        "event_ts": event_ts,
    })

    return events, labels, vecs, event_ids, event_ts


def _time_decay_weight(event_ts_ms: int, now_ms: int) -> float:
    age_days = max(0.0, (now_ms - event_ts_ms) / MS_PER_DAY)
    return math.exp(-age_days / HALF_LIFE_DAYS)


def _knn_raw(
    query_vec: np.ndarray,
    symbol: str,
    horizon_s: int,
    top_k: int,
):
    """
    Returns:
      knn_z, weight_sum, explain_knn
    """
    events, labels, vecs, event_ids, event_ts = _load_labeled_event_vectors_cached()
    if not events or vecs is None:
        if not events:
            return 0.0, 0.0, None

        events2, labels2 = load_labeled_event_vectors()
        if not events2:
            return 0.0, 0.0, None

        vecs2 = np.stack([v for _, _, v in events2])
        sims = cosine_similarity([query_vec], vecs2)[0]
        now_ms = int(time.time() * 1000)

        scored = []
        explain_neighbors = []

        for (eid, ts_ms, _), sim in zip(events2, sims):
            if sim <= 0:
                continue
            key = (eid, symbol, horizon_s)
            if key not in labels2:
                continue

            decay = _time_decay_weight(ts_ms, now_ms)
            w = float(sim) * float(decay)
            if w <= 0:
                continue

            z = labels2[key]
            age_days = (now_ms - ts_ms) / MS_PER_DAY

            scored.append((w, z))
            explain_neighbors.append({
                "event_id": int(eid),
                "sim": float(sim),
                "decay": float(decay),
                "age_days": float(age_days),
                "weight": float(w),
                "impact_z": float(z),
            })

        if not scored:
            return 0.0, 0.0, None

        scored.sort(reverse=True, key=lambda x: x[0])
        explain_neighbors.sort(reverse=True, key=lambda x: x["weight"])

        top = scored[:top_k]
        neighbors = explain_neighbors[:top_k]

        weights = np.array([w for w, _ in top], dtype=float)
        impacts = np.array([z for _, z in top], dtype=float)

        wsum = float(weights.sum())
        if wsum <= 0:
            return 0.0, 0.0, None

        knn_z = float(np.dot(weights, impacts) / wsum)

        explain = {
            "top_k": int(top_k),
            "used": int(len(top)),
            "weight_sum": float(wsum),
            "neighbors": neighbors,
        }

        return knn_z, wsum, explain

    sims = cosine_similarity([query_vec], vecs)[0]
    now_ms = int(time.time() * 1000)

    scored = []
    explain_neighbors = []

    for eid, ts_ms, sim in zip(event_ids, event_ts, sims):
        if sim <= 0:
            continue
        key = (int(eid), str(symbol), int(horizon_s))
        if key not in labels:
            continue

        decay = _time_decay_weight(int(ts_ms), now_ms)
        w = float(sim) * float(decay)
        if w <= 0:
            continue

        z = labels[key]
        age_days = (now_ms - int(ts_ms)) / MS_PER_DAY

        scored.append((w, z))
        explain_neighbors.append({
            "event_id": int(eid),
            "sim": float(sim),
            "decay": float(decay),
            "age_days": float(age_days),
            "weight": float(w),
            "impact_z": float(z),
        })

    if not scored:
        return 0.0, 0.0, None

    scored.sort(reverse=True, key=lambda x: x[0])
    explain_neighbors.sort(reverse=True, key=lambda x: x["weight"])

    top = scored[:top_k]
    neighbors = explain_neighbors[:top_k]

    weights = np.array([w for w, _ in top], dtype=float)
    impacts = np.array([z for _, z in top], dtype=float)

    wsum = float(weights.sum())
    if wsum <= 0:
        return 0.0, 0.0, None

    knn_z = float(np.dot(weights, impacts) / wsum)

    explain = {
        "top_k": int(top_k),
        "used": int(len(top)),
        "weight_sum": float(wsum),
        "neighbors": neighbors,
    }

    return knn_z, wsum, explain


def _blend_with_priors(symbol: str, horizon_s: int, knn_z: float, wsum: float):
    reg_mean, reg_n, reg = get_regime_prior(symbol, horizon_s)
    glob_mean, glob_n = get_global_prior(symbol, horizon_s)

    prior_z = 0.0
    prior_n = 0
    if reg_n > 0:
        prior_z = float(reg_mean)
        prior_n = int(reg_n)
    elif glob_n > 0:
        prior_z = float(glob_mean)
        prior_n = int(glob_n)

    knn_conf = confidence_from_weight(wsum)

    if prior_n <= 0:
        return knn_z, knn_conf, {
            "prior": "none",
            "regime": reg,
            "prior_n": 0,
        }

    prior_conf = confidence_from_n(prior_n)
    prior_strength = 3.0
    alpha = float(wsum / (wsum + prior_strength))

    expected = float(alpha * knn_z + (1.0 - alpha) * prior_z)
    fused = float(
        1.0 - (1.0 - knn_conf) * (1.0 - (0.6 * prior_conf) * (1.0 - alpha))
    )

    explain = {
        "prior": "regime" if reg_n > 0 else "global",
        "regime": reg,
        "prior_n": int(prior_n),
        "alpha": float(alpha),
    }

    return expected, max(0.0, min(1.0, fused)), explain


def _confidence_collapse(confs: list[float]) -> bool:
    if not confs:
        return True
    low = [c for c in confs if c < CONF_COLLAPSE_MIN]
    return (len(low) / max(1, len(confs))) >= CONF_COLLAPSE_FRAC


def predict_event(
    query_vec: np.ndarray,
    symbols: List[str],
    horizons: List[int],
    top_k: int = 8,
    event: Optional[Dict] = None,
) -> Dict[Tuple[str, int], Tuple[float, float, Dict]]:
    """
    Returns:
      (symbol, horizon_s) -> (expected_z, confidence, explain_dict)
    """
    learned = None
    if _USE_LEARNED_REL:
        try:
            learned = learn_relevance_stats(abs_z_threshold=float(_LEARNED_REL_ABS_Z))
        except Exception:
            learned = None

    base: Dict[Tuple[str, int], Tuple[float, float, Dict]] = {}

    for h in horizons:
        for sym in symbols:
            # IMPORTANT:
            # - KNN operates on stored event_embeddings vectors (raw embedding only).
            # - Feature-expansion is used ONLY for the supervised embed regressor path.
            qv_knn = query_vec
            qv_embed = query_vec

            if event is not None:
                feats = build_feature_vector(event=event, symbol=sym)
                qv_embed = np.concatenate([query_vec, np.asarray(feats, dtype=np.float32)])

            knn_z, wsum, knn_ex = _knn_raw(qv_knn, sym, int(h), top_k)

            # ------------------------------------------------------------
            # Option A (OPT-IN): supervised embedding regressor
            # Falls back to KNN automatically if unavailable
            # ------------------------------------------------------------
            embed_pred = None
            if _USE_EMBED_REGRESSOR:
                try:
                    embed_pred = predict_with_embed_model(sym, int(h), qv_embed)
                except Exception:
                    embed_pred = None

            if embed_pred is not None:
                pred_z, n_support, model_ts, model_key_type, model_key, model_kind = embed_pred

                # Confidence from effective support size
                # conf = 1 - exp(-n / k)
                try:
                    n_support = max(0, int(n_support))
                    conf_raw = float(1.0 - math.exp(-n_support / _EMBED_REGRESSOR_CONF_K))
                except Exception:
                    conf_raw = 0.0

                conf_raw = float(max(0.0, min(1.0, conf_raw)))
                conf = float(conf_raw)

                # A2: calibrated confidence (directional correctness proxy)
                if _EMBED_CONF_CALIB:
                    try:
                        con2 = connect()
                        try:
                            curve = _load_embed_conf_calib(con2, int(h), str(model_kind))
                        finally:
                            con2.close()
                        if curve is not None:
                            conf = float(_apply_calib(conf_raw, curve))
                    except Exception:
                        pass

                z = float(pred_z)
                conf = float(max(0.0, min(1.0, conf)))

                explain = {
                    "model": "embed_regressor",
                    "model_kind": str(model_kind),
                    "conf_raw": float(conf_raw),
                    "conf_calibrated": float(conf),
                    "model_key_type": str(model_key_type),
                    "model_key": str(model_key),
                    "model_ts_ms": int(model_ts),
                    "model_n": int(n_support),
                    "fallback_knn": {
                        "knn_z": float(knn_z),
                        "weight_sum": float(wsum),
                        "knn": knn_ex,
                    },
                }

                # Preserve regime/global priors exactly as before
                z2, conf2, prior_ex = _blend_with_priors(sym, int(h), z, float(n_support))
                explain["prior"] = prior_ex

                base[(sym, int(h))] = (float(z2), float(conf2), explain)
                continue

            z, conf, prior_ex = _blend_with_priors(sym, int(h), knn_z, wsum)

            explain = {
                "knn": knn_ex,
                "prior": prior_ex,
            }

            # Option 5.1 (opt-in): scale confidence by learned relevance
            if learned is not None:
                info = (learned.get(str(sym)) or {}).get(int(h)) or {}
                rel = float(info.get("relevance", 0.0))
                n = int(info.get("n", 0))

                m = float(_LEARNED_REL_CONF_FLOOR + (_LEARNED_REL_CONF_CEIL - _LEARNED_REL_CONF_FLOOR) * rel)
                m = max(0.0, min(1.0, m))

                explain["learned_relevance"] = {
                    "abs_z_threshold": float(_LEARNED_REL_ABS_Z),
                    "value": float(rel),
                    "n": int(n),
                    "conf_multiplier": float(m),
                    "applied": bool(_USE_LEARNED_REL),
                }

                if _USE_LEARNED_REL:
                    explain["confidence_base"] = float(conf)
                    conf = float(max(0.0, min(1.0, float(conf) * m)))

            # ------------------------------------------------------------
            # A.2 Drift-aware confidence scaling (prediction unchanged)
            # ------------------------------------------------------------
            drift_scale = 1.0
            con = None
            try:
                con = connect()
                row = con.execute(
                    """
                    SELECT drift_ratio FROM model_drift
                    WHERE symbol=? AND horizon_s=?
                    """,
                    (str(sym), int(h)),
                ).fetchone()
                if row and float(row[0]) > 1.0:
                    drift_scale = float(1.0 / min(3.0, float(row[0])))
            except Exception:
                drift_scale = 1.0
            finally:
                try:
                    if con is not None:
                        con.close()
                except Exception:
                    pass

            explain["drift"] = {
                "applied": True,
                "scale": float(drift_scale),
            }

            conf = float(max(0.0, min(1.0, conf * drift_scale)))

            base[(sym, int(h))] = (float(z), float(conf), explain)

    out: Dict[Tuple[str, int], Tuple[float, float, Dict]] = dict(base)

    for h in horizons:
        zmap = {sym: out[(sym, int(h))][0] for sym in symbols if (sym, int(h)) in out}
        cmap = {sym: out[(sym, int(h))][1] for sym in symbols if (sym, int(h)) in out}

        for target in symbols:
            betas = get_spillover_betas(target, int(h))
            if not betas:
                continue

            adj = 0.0
            used = 0
            contribs = []

            for driver, beta, n in betas:
                if driver not in zmap or int(n) < MIN_BETA_N:
                    continue
                contrib = float(beta) * float(zmap[driver]) * float(min(1.0, cmap[driver]))
                adj += contrib
                used += 1
                contribs.append({
                    "driver": driver,
                    "beta": float(beta),
                    "z_driver": float(zmap[driver]),
                    "conf_driver": float(cmap[driver]),
                    "contrib": float(contrib),
                })

            if used <= 0:
                continue

            z0, c0, ex0 = out[(target, int(h))]
            ex0["spillover"] = {
                "enabled": True,
                "used": int(used),
                "adj": float(adj),
                "contributions": contribs,
            }
            out[(target, int(h))] = (float(z0 + adj), float(c0), ex0)

    # ---- confidence collapse detection ----
    try:
        confs = [float(v[1]) for v in out.values()]
        if _confidence_collapse(confs):
            logging.warning("CONFIDENCE_COLLAPSE detected; falling back to priors")
            # fallback: zero expected_z, confidence from priors only (keep conf bounded)
            for k in list(out.keys()):
                expected_z, conf, explain = out[k]
                out[k] = (0.0, min(1.0, max(0.0, float(conf))), explain)
                if isinstance(explain, dict):
                    explain["fallback"] = "priors_only"
    except Exception:
        pass

    return out


def expected_impact(
    query_vec: np.ndarray,
    symbol: str,
    horizon_s: int,
    top_k: int = 8,
):
    knn_z, wsum, _ = _knn_raw(query_vec, symbol, horizon_s, top_k)
    z, conf, _ = _blend_with_priors(symbol, horizon_s, knn_z, wsum)
    return float(z), float(conf)
