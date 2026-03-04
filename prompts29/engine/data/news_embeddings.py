import os
import threading
import time
import hashlib
from typing import Optional

import numpy as np
from sentence_transformers import SentenceTransformer

from engine.storage import connect

# ---------------------------------------------------------------------------
# Lightweight wrapper around a single shared embedding model instance.
# The model is reused by both the generic event pipeline and our
# "symbol-aware" news embedding helpers below.
# ---------------------------------------------------------------------------

_MODEL_LOCK = threading.Lock()
_MODEL: Optional[SentenceTransformer] = None


def _get_model() -> SentenceTransformer:
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                model_name = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")
                _MODEL = SentenceTransformer(model_name)
    return _MODEL


def embed_text(text: str) -> np.ndarray:
    """Return a normalized float32 embedding for ``text``."""
    if text is None:
        text = ""
    vec = _get_model().encode([text], convert_to_numpy=True, normalize_embeddings=True)
    v: np.ndarray = vec[0].astype(np.float32, copy=False)
    return v


def _symbol_vector(symbol: str) -> np.ndarray:
    """Compute (and cache in-memory) a fixed embedding for a symbol string."""
    return embed_text(symbol)


def _stable_seed(method: str) -> int:
    h = hashlib.sha256(str(method).encode("utf-8", errors="ignore")).hexdigest()
    return int(h[:8], 16)


def _project_back(vec: np.ndarray, out_dim: int, *, method: str) -> np.ndarray:
    """Deterministic random projection back to out_dim.
    Keeps storage size stable even if we use concatenation-based features.
    """
    v = np.asarray(vec, dtype=np.float32, order="C")
    if v.size == int(out_dim):
        return v
    if int(out_dim) <= 0:
        return v
    # seeded, deterministic (not cryptographic)
    rs = np.random.RandomState(_stable_seed(method))
    W = rs.normal(loc=0.0, scale=1.0 / max(1.0, np.sqrt(float(v.size))), size=(int(out_dim), int(v.size))).astype(
        np.float32
    )
    out = W.dot(v)
    return out.astype(np.float32, copy=False)


def _combine(base: np.ndarray, symbol_vec: np.ndarray) -> tuple[np.ndarray, str]:
    """Produce a symbol-aware embedding from the base event vector and symbol vector.

    Method is deterministic and auditable. By default we build a richer representation:
      concat([base, base*sym, abs(base-sym), sym]) then project back to base dim.
    """
    method = os.environ.get("NEWS_SYM_EMB_METHOD", "concat_v1_proj").strip() or "concat_v1_proj"
    base = np.asarray(base, dtype=np.float32)
    sym = np.asarray(symbol_vec, dtype=np.float32)
    if base.size != sym.size:
        # fallback, should not happen for shared embedder
        out = base
    else:
        if method == "mul_v0":
            out = base * sym
        else:
            rich = np.concatenate([base, base * sym, np.abs(base - sym), sym]).astype(np.float32, copy=False)
            out = _project_back(rich, int(base.size), method=method)

    norm = float(np.linalg.norm(out))
    if norm > 0.0 and np.isfinite(norm):
        out = out / norm
    return out.astype(np.float32, copy=False), method


def ensure_symbol_embedding(event_id: int, symbol: str, base_vec: np.ndarray) -> None:
    """Persist a symbol-aware embedding for ``event_id``/``symbol``.
    ``base_vec`` should be the normalized event embedding already stored
    in ``event_embeddings`` (8‑bit float32 blob).
    If the row already exists nothing is changed.
    """
    if not event_id or not symbol or base_vec is None:
        return
    sym = str(symbol).upper().strip()
    try:
        with connect() as con:
            # check if already present
            row = con.execute(
                "SELECT 1 FROM news_symbol_embeddings WHERE event_id=? AND symbol=?",
                (int(event_id), sym),
            ).fetchone()
            if row:
                return
            sym_vec = _symbol_vector(sym)
            comb, method = _combine(base_vec, sym_vec)
            now_ms = int(time.time() * 1000)
            con.execute(
                """
                INSERT OR REPLACE INTO news_symbol_embeddings(event_id, symbol, dim, vec, method, created_ts_ms)
                VALUES (?,?,?,?,?,?)
                """,
                (int(event_id), sym, int(comb.size), comb.tobytes(), str(method), int(now_ms)),
            )
            con.commit()
    except Exception:
        pass


def get_symbol_embedding(event_id: int, symbol: str) -> Optional[np.ndarray]:
    """Fetch a previously computed symbol-aware embedding, or None if missing."""
    if not event_id or not symbol:
        return None
    sym = str(symbol).upper().strip()
    try:
        with connect() as con:
            row = con.execute(
                "SELECT dim, vec FROM news_symbol_embeddings WHERE event_id=? AND symbol=?",
                (int(event_id), sym),
            ).fetchone()
            if not row:
                return None
            dim = int(row[0])
            buf = row[1]
            return np.frombuffer(buf, dtype=np.float32).reshape((dim,))
    except Exception:
        return None


def score_event_symbol(event_id: int, symbol: str) -> float:
    """Return a simple relevance score (dot product) between the event and
    the named symbol.  Caches/creates the embedding as required."""
    try:
        with connect() as con:
            ev = con.execute("SELECT dim, vec FROM event_embeddings WHERE event_id=?", (int(event_id),)).fetchone()
            if not ev:
                return 0.0
            base = np.frombuffer(ev[1], dtype=np.float32)
            sym_vec = _symbol_vector(symbol)
            return float(np.dot(base, sym_vec))
    except Exception:
        return 0.0
