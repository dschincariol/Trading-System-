"""
Process unembedded events:
- embed
- predict expected impact
- store predictions
- emit alerts (with explainability + relevance)

Design goals:
- Ensure core DB schema exists before use
- Avoid holding SQLite write locks during embedding compute
- Include rich explainability payloads
- Heartbeats + job locks for production safety
- Dynamic symbol universe (ACTIVE + WATCH from symbols table)
- Preserve legacy behavior: optional alert confidence downweighting
"""

import re
import time
import os
import json
import random
import logging
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
from sentence_transformers import SentenceTransformer
import torch

# ------------------------------------------------------------
# In-memory cache for recent embeddings (novelty acceleration)
# ------------------------------------------------------------

_RECENT_EMB_CACHE: List[np.ndarray] = []
_RECENT_EMB_CACHE_MAX = int(os.environ.get("NOVELTY_CACHE_MAX", "500"))

# Explicit CPU threading (important on many-core Ryzen)
torch.set_num_threads(int(os.environ.get("TORCH_NUM_THREADS", "16")))
torch.set_num_interop_threads(int(os.environ.get("TORCH_INTEROP_THREADS", "4")))

from dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
    put_event,
)
from dev_core.predictor import predict_event
from dev_core.alerts import emit_alert, init_alerts_db
from dev_core.validation import store_prediction, init_validation_db
from dev_core.decision_log import log_decision, hash_feature_vector
from dev_core.confidence_adjust import get_adjusted_confidence
from dev_core.universe import get_active_symbols
from dev_core.model_v2 import get_current_regime
from dev_core.news_domain import extract_domain, is_domain_blocked, domain_conf_multiplier
from dev_core.symbol_blacklist import is_blacklisted
from dev_core.rules_engine import evaluate_rules
from dev_core.kill_switch import execution_allowed

# ------------------------------------------------------------
# Optional subsystems (shadow-safe)
# ------------------------------------------------------------

try:
    from dev_core.temporal_predictor import predict_temporal_shadow_for_event
except Exception:
    predict_temporal_shadow_for_event = None

try:
    from dev_core.clustering import assign_cluster
except Exception:
    assign_cluster = None

# ------------------------------------------------------------
# Runtime config
# ------------------------------------------------------------

# If symbols table is empty, fallback to a conservative seed set.
DEFAULT_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("DEFAULT_SYMBOLS", "SPY,BTC,OIL").split(",")
    if s.strip()
]

HORIZONS = [300, 3600]

JOB_NAME = "process_events"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15.0"))

# Novelty scoring (embedding-based)
NOVELTY_LOOKBACK = int(os.environ.get("NOVELTY_LOOKBACK", "200"))
NOVELTY_MIN_EVENTS = int(os.environ.get("NOVELTY_MIN_EVENTS", "8"))
NOVELTY_MIN_SCORE = float(os.environ.get("NOVELTY_MIN_SCORE", "0.20"))  # currently explain-only / future gating

# Tradability proxy parameters (explain-only)
RET_SCALE_PER_Z = float(os.environ.get("RET_SCALE_PER_Z", "0.0025"))  # per 1.0 z at 1h (0.25% default)
COST_BPS = float(os.environ.get("COST_BPS", "6.0"))                   # 6 bps default

# ------------------------------------------------------------
# Feature 4: Kill-switch on execution cost spikes (spread-based)
# ------------------------------------------------------------
EXEC_COST_SPIKE_BPS = float(os.environ.get("EXEC_COST_SPIKE_BPS", "45.0"))  # trigger kill if avg spread_bps >= this
EXEC_COST_SPIKE_WINDOW_S = int(os.environ.get("EXEC_COST_SPIKE_WINDOW_S", "120"))  # lookback window
EXEC_COST_SPIKE_MIN_N = int(os.environ.get("EXEC_COST_SPIKE_MIN_N", "8"))          # minimum samples required
EXEC_COST_SPIKE_SYMBOL_LIMIT = int(os.environ.get("EXEC_COST_SPIKE_SYMBOL_LIMIT", "50"))  # top symbols to sample

# Preserve legacy alert behavior (old file): downweight alert confidence if expected_ret_net < 0
ALERT_DOWNWEIGHT_NEG_NET = os.environ.get("ALERT_DOWNWEIGHT_NEG_NET", "1") == "1"
ALERT_DOWNWEIGHT_MULT = float(os.environ.get("ALERT_DOWNWEIGHT_MULT", "0.75"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
DISCOVER_SYMBOLS = os.environ.get("DISCOVER_SYMBOLS", "1") == "1"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [process_events] %(message)s",
)

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

def _sleep_with_jitter(seconds: float) -> None:
    if seconds <= 0:
        return
    j = seconds * 0.2
    time.sleep(max(0.05, seconds + random.uniform(-j, j)))


def _features_hash(vec: np.ndarray) -> str:
    return hash_feature_vector(vec)


def _cosine_max_sim(vec: np.ndarray, mat: np.ndarray) -> float:
    """
    vec: (d,) float32
    mat: (n,d) float32
    returns max cosine similarity in [-1,1]
    """
    if mat is None or mat.size == 0:
        return 0.0
    v = vec.astype(np.float32, copy=False)
    m = mat.astype(np.float32, copy=False)

    vnorm = float(np.linalg.norm(v))
    if vnorm <= 1e-12:
        return 0.0

    mn = np.linalg.norm(m, axis=1)
    good = mn > 1e-12
    if not np.any(good):
        return 0.0

    sims = (m[good] @ v) / (mn[good] * vnorm)
    if sims.size == 0:
        return 0.0
    return float(np.max(sims))

def _compute_novelty(con, event_id: int, vec: np.ndarray, lookback: int) -> float:
    """
    Novelty = 1 - max cosine similarity to recent embedded events.
    Uses in-memory cache first, DB as fallback.
    """
    try:
        # --- Fast path: in-memory cache ---
        if len(_RECENT_EMB_CACHE) >= max(1, int(NOVELTY_MIN_EVENTS)):
            mat = np.vstack(_RECENT_EMB_CACHE[-lookback:]).astype(np.float32, copy=False)
            max_sim = _cosine_max_sim(vec, mat)
            novelty = 1.0 - max_sim
            if novelty == novelty:
                return float(max(0.0, min(1.0, novelty)))
    except Exception:
        pass

    # --- Fallback: DB lookup ---
    try:
        rows = con.execute(
            """
            SELECT emb.dim, emb.vec
            FROM event_embeddings emb
            JOIN events e ON e.id = emb.event_id
            WHERE emb.event_id != ?
            ORDER BY e.ts_ms DESC
            LIMIT ?
            """,
            (int(event_id), int(lookback)),
        ).fetchall()
    except Exception:
        return 0.0

    if not rows or len(rows) < max(1, int(NOVELTY_MIN_EVENTS)):
        return 0.0

    try:
        mats: List[np.ndarray] = []
        d0 = int(rows[0][0] or 0)
        if d0 <= 0:
            return 0.0

        for dim, blob in rows:
            if int(dim or 0) != d0 or not blob:
                continue
            a = np.frombuffer(blob, dtype=np.float32)
            if a.size == d0:
                mats.append(a)

        if len(mats) < max(1, int(NOVELTY_MIN_EVENTS)):
            return 0.0

        mat = np.vstack(mats).astype(np.float32, copy=False)
        max_sim = _cosine_max_sim(vec, mat)
        novelty = 1.0 - max_sim
        if novelty != novelty:
            return 0.0
        return float(max(0.0, min(1.0, novelty)))
    except Exception:
        return 0.0

def _update_event_meta_json(con, event_id: int, meta: Dict[str, Any]) -> None:
    """
    Best-effort UPDATE events.meta_json. Fail-soft if column doesn't exist.
    """
    try:
        con.execute(
            "UPDATE events SET meta_json=? WHERE id=?",
            (json.dumps(meta or {}, separators=(",", ":"), sort_keys=True), int(event_id)),
        )
    except Exception:
        pass

def _exec_cost_context(con, symbol: str) -> Dict[str, Any]:
    try:
        row = con.execute(
            """
            SELECT ts_ms, last, bid, ask, spread, source
            FROM price_quotes
            WHERE symbol=?
            ORDER BY ts_ms DESC
            LIMIT 1
            """,
            (str(symbol),),
        ).fetchone()

        if not row:
            return {}

        ts_ms, last, bid, ask, spread, src = row

        if spread is None and bid is not None and ask is not None:
            spread = float(ask) - float(bid)

        spread_bps = None
        if last and spread and last > 1e-9:
            spread_bps = 10000.0 * float(spread) / float(last)

        return {
            "ts_ms": int(ts_ms),
            "last": float(last) if last is not None else None,
            "bid": float(bid) if bid is not None else None,
            "ask": float(ask) if ask is not None else None,
            "spread": float(spread) if spread is not None else None,
            "spread_bps": float(spread_bps) if spread_bps is not None else None,
            "source": src,
        }
    except Exception:
        return {}

def _detect_exec_cost_spike(con) -> Dict[str, Any]:
    """
    Returns:
      {
        "spike": bool,
        "avg_spread_bps": float|None,
        "p90_spread_bps": float|None,
        "n": int,
        "window_s": int,
        "cutoff_ts_ms": int,
        "symbols": [..]  # sampled symbols
      }
    """
    now_ms = int(time.time() * 1000)
    cutoff = int(now_ms - int(EXEC_COST_SPIKE_WINDOW_S) * 1000)

    # Sample most recently updated ACTIVE/WATCH symbols (bounded)
    syms = []
    try:
        rows = con.execute(
            """
            SELECT symbol
            FROM symbols
            WHERE status IN ('ACTIVE','WATCH')
            ORDER BY updated_ts_ms DESC
            LIMIT ?
            """,
            (int(max(1, EXEC_COST_SPIKE_SYMBOL_LIMIT)),),
        ).fetchall()
        for r in rows or []:
            if r and r[0]:
                syms.append(str(r[0]))
    except Exception:
        syms = []

    if not syms:
        return {
            "spike": False,
            "avg_spread_bps": None,
            "p90_spread_bps": None,
            "n": 0,
            "window_s": int(EXEC_COST_SPIKE_WINDOW_S),
            "cutoff_ts_ms": int(cutoff),
            "symbols": [],
        }

    spreads = []
    try:
        q = ",".join(["?"] * len(syms))
        rows = con.execute(
            f"""
            SELECT last, bid, ask, spread
            FROM price_quotes
            WHERE ts_ms >= ?
              AND symbol IN ({q})
            """,
            (int(cutoff), *syms),
        ).fetchall()

        for last, bid, ask, spr in rows or []:
            try:
                last = float(last) if last is not None else None
                if last is None or last <= 1e-9:
                    continue

                if spr is None and bid is not None and ask is not None:
                    spr = float(ask) - float(bid)
                if spr is None:
                    continue

                spr = float(spr)
                sbps = 10000.0 * spr / last
                if sbps == sbps and sbps >= 0.0:
                    spreads.append(float(sbps))
            except Exception:
                continue
    except Exception:
        spreads = []

    n = int(len(spreads))
    if n < int(max(1, EXEC_COST_SPIKE_MIN_N)):
        return {
            "spike": False,
            "avg_spread_bps": (float(np.mean(spreads)) if spreads else None),
            "p90_spread_bps": (float(np.percentile(spreads, 90)) if spreads else None),
            "n": int(n),
            "window_s": int(EXEC_COST_SPIKE_WINDOW_S),
            "cutoff_ts_ms": int(cutoff),
            "symbols": list(syms),
        }

    avg_bps = float(np.mean(spreads))
    p90_bps = float(np.percentile(spreads, 90))

    spike = avg_bps >= float(EXEC_COST_SPIKE_BPS)

    return {
        "spike": bool(spike),
        "avg_spread_bps": float(avg_bps),
        "p90_spread_bps": float(p90_bps),
        "n": int(n),
        "window_s": int(EXEC_COST_SPIKE_WINDOW_S),
        "cutoff_ts_ms": int(cutoff),
        "symbols": list(syms),
    }

def _options_context(con, symbol: str) -> Dict[str, float]:
    """
    Returns lightweight IV/OI context for explain + confidence shaping.
    """
    try:
        row = con.execute(
            """
            SELECT AVG(iv), SUM(open_interest)
            FROM options_chain
            WHERE symbol=?
              AND ts_ms >= ?
            """,
            (str(symbol), int(time.time() * 1000) - 3600_000),
        ).fetchone()
        if not row:
            return {}
        return {
            "avg_iv": float(row[0]) if row[0] is not None else None,
            "open_interest": int(row[1]) if row[1] is not None else None,
        }
    except Exception:
        return {}

def _options_anomaly(con, symbol: str) -> Dict[str, float]:
    """
    Compute anomaly signals from options_chain:
      - iv_ratio_1h_24h
      - oi_delta_1h_24h
    """
    now_ms = int(time.time() * 1000)
    h1 = now_ms - 3600_000
    h24 = now_ms - 24 * 3600_000

    try:
        r1 = con.execute(
            """
            SELECT AVG(iv), AVG(open_interest)
            FROM options_chain
            WHERE symbol=? AND ts_ms >= ?
            """,
            (str(symbol), int(h1)),
        ).fetchone()
        r24 = con.execute(
            """
            SELECT AVG(iv), AVG(open_interest)
            FROM options_chain
            WHERE symbol=? AND ts_ms >= ?
            """,
            (str(symbol), int(h24)),
        ).fetchone()
    except Exception:
        return {}

    try:
        iv1 = float(r1[0]) if r1 and r1[0] is not None else None
        iv24 = float(r24[0]) if r24 and r24[0] is not None else None
        oi1 = float(r1[1]) if r1 and r1[1] is not None else None
        oi24 = float(r24[1]) if r24 and r24[1] is not None else None
    except Exception:
        return {}

    out = {}

    if iv1 is not None and iv24 is not None and iv24 > 1e-12:
        out["iv_ratio_1h_24h"] = float(iv1 / iv24)

    if oi1 is not None and oi24 is not None:
        out["oi_delta_1h_24h"] = float(oi1 - oi24)

    return out

def _earnings_context(con, symbol: str) -> Dict[str, Any]:
    """
    Returns next earnings date if within lookahead window.
    """
    try:
        look_days = int(os.environ.get("EARNINGS_EVENT_LOOKAHEAD_DAYS", "10"))
    except Exception:
        look_days = 10

    try:
        # date strings are YYYY-MM-DD; SQLite text compares correctly
        row = con.execute(
            """
            SELECT earnings_date, time_of_day, eps_est, revenue_est, source
            FROM earnings_calendar
            WHERE symbol=?
              AND earnings_date >= date('now')
              AND earnings_date <= date('now', ?)
            ORDER BY earnings_date ASC
            LIMIT 1
            """,
            (str(symbol), f"+{int(look_days)} day"),
        ).fetchone()
        if not row:
            return {}
        return {
            "earnings_date": str(row[0]),
            "time_of_day": (str(row[1]) if row[1] is not None else None),
            "eps_est": (float(row[2]) if row[2] is not None else None),
            "revenue_est": (float(row[3]) if row[3] is not None else None),
            "source": (str(row[4]) if row[4] is not None else None),
        }
    except Exception:
        return {}


def _sec_filing_context(con, symbol: str) -> Dict[str, Any]:
    """
    Returns most recent filing in last N days (default 3).
    """
    try:
        look_days = int(os.environ.get("SEC_FILING_LOOKBACK_DAYS", "3"))
    except Exception:
        look_days = 3

    try:
        row = con.execute(
            """
            SELECT form, filed_date, accession, primary_doc_url, source
            FROM sec_filings
            WHERE symbol=?
              AND filed_date >= date('now', ?)
            ORDER BY filed_date DESC
            LIMIT 1
            """,
            (str(symbol), f"-{int(look_days)} day"),
        ).fetchone()
        if not row:
            return {}
        return {
            "form": str(row[0]),
            "filed_date": str(row[1]),
            "accession": str(row[2]),
            "primary_doc_url": (str(row[3]) if row[3] is not None else None),
            "source": (str(row[4]) if row[4] is not None else None),
        }
    except Exception:
        return {}

def _tradability_from_pred(expected_z: float, horizon_s: int, novelty: float) -> Dict[str, float]:
    """
    Convert model output (impact z) into tradability proxies.
    Explain-only, intentionally conservative.
    """
    try:
        z = float(expected_z)
    except Exception:
        z = 0.0
    try:
        h = max(1, int(horizon_s))
    except Exception:
        h = 3600

    # Adjust horizon by sqrt(time) relative to 1h baseline
    h_scale = (h / 3600.0) ** 0.5

    expected_ret = z * float(RET_SCALE_PER_Z) * float(h_scale)
    expected_cost = float(COST_BPS) / 10000.0

    # Win-prob proxy: monotonic in z + modest novelty boost
    p_win = 0.5 + 0.15 * max(-3.0, min(3.0, z))
    p_win += 0.05 * max(0.0, min(1.0, float(novelty)))
    p_win = max(0.0, min(1.0, p_win))

    expected_dd = abs(z) * 0.001 * float(h_scale)

    return {
        "p_win": float(p_win),
        "expected_ret": float(expected_ret),
        "expected_cost": float(expected_cost),
        "expected_ret_net": float(expected_ret - expected_cost),
        "expected_dd": float(expected_dd),
    }

# ------------------------------------------------------------
# Heuristic relevance rules (title-only)
# ------------------------------------------------------------

_RELEVANCE_RULES = {
    "BTC": [
        ("crypto keywords", r"\b(bitcoin|btc|crypto|cryptocurrency|ethereum|eth|defi|blockchain)\b"),
        ("major exchanges", r"\b(coinbase|binance|kraken)\b"),
        ("regulation / ETF", r"\b(sec|etf|spot etf|crypto etf|regulat(ion|ory))\b"),
        ("stablecoins", r"\b(stablecoin|usdt|tether|usdc)\b"),
    ],
    "OIL": [
        ("oil keywords", r"\b(oil|crude|wti|brent)\b"),
        ("OPEC / supply", r"\b(opec|opec\+|production cut|output)\b"),
        ("energy / gasoline", r"\b(energy|gasoline|diesel|refiner(y|ies)|pipeline)\b"),
        ("geopolitics", r"\b(iran|iraq|saudi|russia|ukraine|middle east|red sea)\b"),
    ],
    "SPY": [
        ("macro policy", r"\b(fed|fomc|rates?|hike|cut|qt|qe|powell)\b"),
        ("inflation / jobs", r"\b(cpi|pce|inflation|nfp|payrolls?|unemployment|jobs report)\b"),
        ("bonds / yields", r"\b(yield(s)?|treasur(y|ies)|bond(s)?|curve)\b"),
        ("equities broad", r"\b(sp\s*500|s&p|nasdaq|dow|equities?|stocks?)\b"),
        ("growth / recession", r"\b(recession|gdp|soft landing|hard landing)\b"),
        ("earnings broad", r"\b(earnings|guidance)\b"),
    ],
}

_COMPILED = {
    sym: [(label, re.compile(pat, flags=re.IGNORECASE)) for (label, pat) in rules]
    for sym, rules in _RELEVANCE_RULES.items()
}


def _score_from_hit_count(n: int) -> float:
    if n <= 0:
        return 0.0
    if n == 1:
        return 0.35
    if n == 2:
        return 0.60
    return 0.85


def relevance_for_title(title: str, symbol: str) -> Tuple[float, List[str]]:
    t = (title or "").strip()
    if not t:
        return 0.0, []
    reasons: List[str] = []
    for label, rx in _COMPILED.get(symbol, []):
        try:
            if rx.search(t):
                reasons.append(label)
        except re.error:
            continue
    return _score_from_hit_count(len(reasons)), reasons

# ------------------------------------------------------------
# Symbol discovery (WATCH-only)
# ------------------------------------------------------------

_SYMBOL_PATTERNS = [
    # Equities / ETFs
    r"\b([A-Z]{2,5})\b",
    # Crypto tickers
    r"\b(BTC|ETH|SOL|BNB|XRP|ADA|AVAX|DOT|LINK)\b",
]

def discover_symbols_from_text(text: str):
    if not text:
        return set()
    out = set()
    for pat in _SYMBOL_PATTERNS:
        try:
            for m in re.findall(pat, text):
                sym = m if isinstance(m, str) else m[0]
                if 2 <= len(sym) <= 6:
                    out.add(sym.upper())
        except Exception:
            continue
    return out

def upsert_watch_symbols(con, symbols, ts_ms: int):
    for sym in symbols:
        try:
            con.execute(
                """
                INSERT INTO symbol_universe(symbol, status, first_seen_ms, last_seen_ms, seen_n)
                VALUES (?, 'WATCH', ?, ?, 1)
                ON CONFLICT(symbol) DO UPDATE SET
                  last_seen_ms=excluded.last_seen_ms,
                  seen_n=seen_n+1
                """,
                (str(sym), int(ts_ms), int(ts_ms)),
            )
        except Exception:
            pass

def relevance_map(title: str, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for sym in symbols:
        score, reasons = relevance_for_title(title, sym)
        out[sym] = {"score": float(score), "reasons": list(reasons)}
    return out

# ------------------------------------------------------------
# Embedding model (lazy init)
# ------------------------------------------------------------

_model: Optional[SentenceTransformer] = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer("all-MiniLM-L6-v2")
    return _model

# ------------------------------------------------------------
# Ensure schemas exist
# ------------------------------------------------------------

init_db()
init_alerts_db()
init_validation_db()

# ------------------------------------------------------------
# Main loop
# ------------------------------------------------------------

def main() -> None:
    if not acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        raise SystemExit(2)

    # ---------------------------------------------------------
    # Rules engine (single pass)
    # ---------------------------------------------------------
    try:
        evaluate_rules()
    except Exception:
        pass

    last_hb_s = 0.0
    started_ms = int(time.time() * 1000)

    try:
        # --------------------------------------------------------
        # Phase 3: Global rules engine (auto kill-switch)
        # --------------------------------------------------------
        try:
            evaluate_rules()
        except Exception:
            pass

        if not execution_allowed():
            logging.warning("execution blocked by rules engine; sleeping")
            _sleep_with_jitter(2.0)
            return
        # --------------------------------------------------------
        # Phase 5: rules engine (per-symbol halts + global stops)
        # --------------------------------------------------------

        allow0, _, _ = execution_allowed(symbol=None, regime=None)
        if not allow0:
            logging.warning("execution blocked by kill switch; exiting process_events pass")
            return

        # --------------------------------------------------------
        # Phase 5/6/7: rules engine (global + per-symbol halts)
        # --------------------------------------------------------

        allow0, _, _ = execution_allowed(symbol=None, regime=None)
        if not allow0:
            logging.warning("execution blocked by kill switch; exiting process_events pass")
            return

        # Load dynamic universe (ACTIVE + WATCH). Fallback if empty.
        conu = connect()
        try:
            try:
                symbols = get_active_symbols(conu, limit=int(os.environ.get("PROCESS_SYMBOL_LIMIT", "2000")))
            except Exception:
                symbols = []

            if not symbols:
                symbols = list(DEFAULT_SYMBOLS)

            symbols = list(dict.fromkeys(symbols))  # de-dup, preserve order

            # --------------------------------------------------------
            # Feature 4: Kill-switch on execution cost spikes (spread)
            # --------------------------------------------------------
            try:
                spike_info = _detect_exec_cost_spike(conu)
                if spike_info and spike_info.get("spike"):
                    # hard stop this pass (prevents any downstream action)
                    logging.error("EXEC_COST_SPIKE spike_info=%s", spike_info)

                    # Best-effort operator-visible alert (if alerts subsystem uses symbols)
                    try:
                        emit_alert(
                            event_title="Execution cost spike — trading halted",
                            symbol="SPY",
                            horizon_s=0,
                            expected_z=0.0,
                            confidence=1.0,
                            explain={
                                "type": "exec_cost_spike",
                                "spike_info": spike_info,
                                "threshold_bps": float(EXEC_COST_SPIKE_BPS),
                            },
                        )
                    except Exception:
                        pass

                    return
            except Exception:
                pass

            # Preload symbol status map (avoid per-symbol DB queries)
            symbol_status = {}
            try:
                rows = conu.execute("SELECT symbol, status FROM symbol_universe").fetchall()
                for s, st in rows or []:
                    symbol_status[str(s)] = str(st)
            except Exception:
                symbol_status = {}
        finally:
            try:
                conu.close()
            except Exception:
                pass

        # --------------------------------------------------------
        # Read candidate events (no write txn)
        # --------------------------------------------------------
        con = connect()
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
            logging.info("no new events to process")
            return

        # Embed outside write transaction
        titles = [(r[3] or "") for r in rows]
        embeddings = _get_model().encode(
            titles,
            batch_size=int(os.environ.get("EMBED_BATCH_SIZE", "64")),
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        ).astype(np.float32, copy=False)

        conw = connect()
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
                                {"event_id": int(eid), "event_ts_ms": int(ts_ms)},
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                        )

                    except Exception:
                        pass
                    last_hb_s = now_s

                title = title or ""
                body = body or ""
                source = source or ""
                url = url or ""
                ts_ms = int(ts_ms or 0)

                try:
                    event_meta = json.loads(meta_json) if meta_json else {}
                except Exception:
                    event_meta = {}
                if not isinstance(event_meta, dict):
                    event_meta = {}

                domain = extract_domain(url, meta_json)
                if domain:
                    event_meta["domain"] = domain

                logging.info("EVENT id=%s ts_ms=%s title=%s", eid, ts_ms, title)

                # WATCH-only symbol discovery from text (never trades by itself)
                if DISCOVER_SYMBOLS:
                    try:
                        syms = set()
                        syms |= discover_symbols_from_text(title or "")
                        syms |= discover_symbols_from_text(body or "")
                        if syms:
                            upsert_watch_symbols(conw, syms, int(ts_ms))
                    except Exception:
                        pass

                rel_map = relevance_map(title, symbols)
                event_ctx = {
                    "event_id": int(eid),
                    "ts_ms": int(ts_ms),
                    "source": source,
                    "title": title,
                    "body": body,
                    "url": url,
                    "meta": event_meta,
                }

                preds = predict_event(
                    vec,
                    symbols,
                    HORIZONS,
                    top_k=8,
                    event=event_ctx,
                )


                # Temporal predictor (shadow-mode only)
                temporal_shadow = None
                if predict_temporal_shadow_for_event:
                    try:
                        temporal_shadow = predict_temporal_shadow_for_event(
                            conw,
                            event_id=int(eid),
                            ts_ms=int(ts_ms),
                            symbols=symbols,
                            horizons=HORIZONS,
                        )
                    except Exception:
                        temporal_shadow = None

                # Persist embedding
                cur.execute(
                    "INSERT OR REPLACE INTO event_embeddings(event_id, dim, vec) VALUES (?,?,?)",
                    (int(eid), int(len(vec)), vec.tobytes()),
                )

                # Update in-memory novelty cache (best-effort)
                _RECENT_EMB_CACHE.append(vec)
                if len(_RECENT_EMB_CACHE) > _RECENT_EMB_CACHE_MAX:
                    _RECENT_EMB_CACHE.pop(0)

                # Novelty scoring — compute + persist
                novelty = 0.0
                try:
                    novelty = _compute_novelty(conw, event_id=int(eid), vec=vec, lookback=int(NOVELTY_LOOKBACK))
                except Exception:
                    novelty = 0.0

                event_meta["novelty"] = float(novelty)
                _update_event_meta_json(conw, event_id=int(eid), meta=event_meta)

                # Optional clustering
                cluster_info = None
                if assign_cluster:
                    try:
                        cluster_info = assign_cluster(event_id=int(eid), ts_ms=int(ts_ms), title=title, vec=vec)
                    except Exception:
                        cluster_info = None

                # Store predictions + decisions + alerts
                for sym in symbols:
                    # Skip symbols that are currently non-tradable
                    st = symbol_status.get(sym)
                    if st in ("DISABLED", "COOLDOWN"):
                        continue

                    for h in HORIZONS:
                        expected_z, conf, explain = preds[(sym, int(h))]
                        base_conf = float(conf)
                        adj_conf = float(base_conf)
                        adj_explain = {}
                        explain = dict(explain or {})

                        # attach event metadata for UI/modeling
                        if event_meta:
                            explain["event_meta"] = event_meta

                        r = rel_map.get(sym) or {}
                        explain["relevance"] = float(r.get("score", 0.0))
                        explain["relevance_reasons"] = list(r.get("reasons", []))

                        explain["tradability"] = _tradability_from_pred(
                            expected_z=float(expected_z),
                            horizon_s=int(h),
                            novelty=float(novelty),
                        )

                        # Options IV / OI context (explain + confidence shaping)
                        opt_ctx = _options_context(conw, sym)
                        if opt_ctx:
                            explain["options"] = opt_ctx

                        # 5.2: News × Options anomaly interaction features
                        opt_anom = _options_anomaly(conw, sym)
                        if opt_anom:
                            explain["options_anomaly"] = opt_anom

                        # 5.3: regime-specific news sensitivity (regime is per-symbol)
                        reg = get_current_regime(sym)
                        explain["regime"] = str(reg)

                        # 5.4: domain blacklist (global + per-symbol)
                        if domain and is_domain_blocked(domain, sym):
                            continue

                        if domain:
                            explain["domain"] = domain

                        # Earnings + SEC filings context (explain + confidence shaping)
                        earn_ctx = _earnings_context(conw, sym)
                        if earn_ctx:
                            explain["earnings"] = earn_ctx

                        filing_ctx = _sec_filing_context(conw, sym)
                        if filing_ctx:
                            explain["sec_filing"] = filing_ctx

                        explain.setdefault("event", {"event_id": int(eid), "title": title})
                        explain.setdefault("relevance_map", rel_map)
                        if cluster_info:
                            explain["cluster"] = cluster_info

                        # ---------------------------------------------------------
                        # Feature 3: Spread-aware confidence decay
                        # ---------------------------------------------------------
                        try:
                            cost_ctx = _exec_cost_context(conw, sym)
                            if cost_ctx:
                                explain["exec_cost"] = cost_ctx

                                sbps = cost_ctx.get("spread_bps")
                                if sbps is not None:
                                    sbps = float(sbps)

                                    hard_bps = float(os.environ.get("SPREAD_HARD_BLOCK_BPS", "80"))
                                    soft_bps = float(os.environ.get("SPREAD_SOFT_START_BPS", "15"))
                                    max_decay = float(os.environ.get("SPREAD_MAX_DECAY", "0.60"))
                                    slope = float(os.environ.get("SPREAD_DECAY_SLOPE", "0.01"))

                                    if sbps >= hard_bps:
                                        continue

                                    if sbps > soft_bps:
                                        excess = sbps - soft_bps
                                        mult = 1.0 - slope * excess
                                        if mult < max_decay:
                                            mult = max_decay
                                        adj_conf = adj_conf * mult
                                        explain["spread_conf_mult"] = float(mult)
                                    else:
                                        explain["spread_conf_mult"] = 1.0
                        except Exception:
                            pass


                        # Temporal predictor payload (shadow-only)
                        if isinstance(temporal_shadow, dict):
                            try:
                                k = (str(sym).upper().strip(), int(h))
                                if k in temporal_shadow:
                                    tz, tconf, texplain = temporal_shadow[k]
                                    explain["temporal_shadow"] = {
                                        "predicted_z": float(tz),
                                        "confidence": float(tconf),
                                        "explain": (texplain or {}),
                                    }
                            except Exception:
                                pass

                        # Always store prediction + decision log (independent of temporal shadow)
                        store_prediction(
                            event_id=eid,
                            symbol=sym,
                            horizon_s=int(h),
                            predicted_z=float(expected_z),
                            confidence=float(adj_conf),
                        )

                        # ------------------------------------------------
                        # Confidence adjustments (staleness / vol proxy)
                        # ------------------------------------------------
                        try:
                            adj_conf, adj_explain = get_adjusted_confidence(
                                conw,
                                symbol=sym,
                                horizon_s=int(h),
                                base_conf=float(adj_conf),
                            )
                        except Exception:
                            pass

                        explain["confidence_adjust"] = adj_explain
                        if opt_ctx and opt_ctx.get("avg_iv"):
                            iv = float(opt_ctx["avg_iv"])
                            if iv > 1.0:  # extreme implied vol
                                adj_conf = adj_conf * 0.85

                        # Earnings proximity downweight (scheduled catalyst)
                        if earn_ctx and earn_ctx.get("earnings_date"):
                            try:
                                adj_conf = adj_conf * float(os.environ.get("EARNINGS_CONF_DOWNWEIGHT", "0.85"))
                            except Exception:
                                adj_conf = adj_conf * 0.85

                        # Recent SEC filing downweight (unscheduled info risk, esp 8-K)
                        if filing_ctx and filing_ctx.get("form"):
                            form = str(filing_ctx.get("form") or "").upper()
                            if form == "8-K":
                                try:
                                    adj_conf = adj_conf * float(os.environ.get("SEC_8K_CONF_DOWNWEIGHT", "0.90"))
                                except Exception:
                                    adj_conf = adj_conf * 0.90

                        # 5.2 interaction: if IV is spiking vs baseline, be more conservative
                        try:
                            if opt_anom and opt_anom.get("iv_ratio_1h_24h") is not None:
                                ivr = float(opt_anom["iv_ratio_1h_24h"])
                                if ivr >= float(os.environ.get("IV_SPIKE_RATIO", "1.6")):
                                    adj_conf = adj_conf * float(os.environ.get("IV_SPIKE_CONF_DOWNWEIGHT", "0.88"))
                        except Exception:
                            pass

                        # 5.3 regime-specific domain sensitivity multiplier (learned later)
                        try:
                            if domain:
                                mult = domain_conf_multiplier(domain, sym, reg, int(h))
                                adj_conf = adj_conf * float(mult)
                                explain["domain_conf_mult"] = float(mult)
                        except Exception:
                            pass

                        explain["adjusted_confidence"] = float(adj_conf)

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

                        # Emit alert (preserve legacy optional downweight behavior)
                        alert_conf = float(adj_conf)
                        if ALERT_DOWNWEIGHT_NEG_NET:
                            try:
                                tr = explain.get("tradability") or {}
                                net = float(tr.get("expected_ret_net") or 0.0)
                                if net < 0.0:
                                    alert_conf = alert_conf * float(ALERT_DOWNWEIGHT_MULT)
                            except Exception:
                                pass
                        # Feature 4: stop emitting alerts if execution costs spike mid-pass
                        try:
                            spike_info2 = _detect_exec_cost_spike(conw)
                            if spike_info2 and spike_info2.get("spike"):
                                logging.error("EXEC_COST_SPIKE mid_pass spike_info=%s", spike_info2)
                                continue
                        except Exception:
                            pass

                        if not execution_allowed():
                            continue

                        allow_sym, _, _ = execution_allowed(symbol=sym, regime=None)
                        if not allow_sym:
                            continue

                        try:
                            emit_alert(

                                event_title=title,
                                symbol=sym,
                                horizon_s=int(h),
                                expected_z=float(expected_z),
                                confidence=float(alert_conf),
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
        logging.info("PROCESS COMPLETE dur_ms=%s", dur_ms)

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
