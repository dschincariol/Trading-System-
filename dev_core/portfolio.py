# =========================
# SECTION 1 / ~200 lines
# =========================
# dev_core/portfolio.py
"""
Portfolio / Strategy layer (paper-trading / intent only)

Reads recent ALERTS (already quality-gated) and produces:
- target positions (weights)
- order intents (delta from current state)

Design goals:
- Minimal & production-safe (SQLite only)
- No broker execution
- Explainable decisions
- Anti-flip-flop: min hold time before reversing
"""

import json
import os
import time
import math
from typing import Dict, List, Optional, Tuple

from dev_core.storage import connect
from dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from dev_core.strategy_selector import choose_strategy_name, load_strategy_module
from dev_core.universe import get_active_symbols
from dev_core.symbol_blacklist import is_blacklisted
from dev_core.portfolio_risk_gate import apply_portfolio_risk_gate
from dev_core.risk_state import get_state
from dev_core.factor_universe import _get_feature_asof as _get_factor_feature_asof

# -----------------------------
# Strategy controls (env)
# -----------------------------

# Only consider alerts within lookback window
PORTFOLIO_LOOKBACK_S = int(os.environ.get("PORTFOLIO_LOOKBACK_S", "21600"))  # 6h

# Must pass these to be tradable
PORTFOLIO_MIN_CONF = float(os.environ.get("PORTFOLIO_MIN_CONF", "0.60"))
PORTFOLIO_MIN_ABS_Z = float(os.environ.get("PORTFOLIO_MIN_ABS_Z", "1.25"))

# Max number of concurrent positions
PORTFOLIO_MAX_POSITIONS = int(os.environ.get("PORTFOLIO_MAX_POSITIONS", "3"))
# Dynamic universe promotion gates
UNIVERSE_MIN_SEEN = int(os.environ.get("UNIVERSE_MIN_SEEN", "3"))
UNIVERSE_MIN_PRICE_AGE_S = int(os.environ.get("UNIVERSE_MIN_PRICE_AGE_S", "180"))
UNIVERSE_MIN_VOLUME = float(os.environ.get("UNIVERSE_MIN_VOLUME", "0"))
UNIVERSE_MAX_PROMOTIONS_PER_DAY = int(os.environ.get("UNIVERSE_MAX_PROMOTIONS_PER_DAY", "3"))

# Gross exposure cap (sum of abs weights)
PORTFOLIO_GROSS_CAP = float(os.environ.get("PORTFOLIO_GROSS_CAP", "1.00"))

# -----------------------------
# Capital Preservation Mode (CPM)
# -----------------------------
PORTFOLIO_PRESERVE_GROSS_FACTOR = float(os.environ.get("PORTFOLIO_PRESERVE_GROSS_FACTOR", "0.35"))
PORTFOLIO_PRESERVE_MIN_CONF_ADD = float(os.environ.get("PORTFOLIO_PRESERVE_MIN_CONF_ADD", "0.10"))
PORTFOLIO_PRESERVE_MIN_ABS_Z_ADD = float(os.environ.get("PORTFOLIO_PRESERVE_MIN_ABS_Z_ADD", "0.25"))
PORTFOLIO_PRESERVE_MAX_POSITIONS = int(os.environ.get("PORTFOLIO_PRESERVE_MAX_POSITIONS", "1"))
PORTFOLIO_PRESERVE_REBALANCE_COOLDOWN_MULT = float(os.environ.get("PORTFOLIO_PRESERVE_REBALANCE_COOLDOWN_MULT", "3.0"))


def _capital_mode() -> str:
    try:
        return str(get_state("capital_mode", "normal") or "normal")
    except Exception:
        return "normal"


def _eff_min_conf() -> float:
    v = float(PORTFOLIO_MIN_CONF)
    if _capital_mode() == "preserve":
        v = min(0.99, v + float(PORTFOLIO_PRESERVE_MIN_CONF_ADD))
    return float(v)


def _eff_min_abs_z() -> float:
    v = float(PORTFOLIO_MIN_ABS_Z)
    if _capital_mode() == "preserve":
        v = v + float(PORTFOLIO_PRESERVE_MIN_ABS_Z_ADD)
    return float(v)


def _eff_max_positions() -> int:
    v = int(PORTFOLIO_MAX_POSITIONS)
    if _capital_mode() == "preserve":
        v = int(max(0, int(PORTFOLIO_PRESERVE_MAX_POSITIONS)))
    return int(v)


def _eff_gross_cap() -> float:
    v = float(PORTFOLIO_GROSS_CAP)
    if _capital_mode() == "preserve":
        v = float(v) * float(max(0.0, min(1.0, float(PORTFOLIO_PRESERVE_GROSS_FACTOR))))
    return float(v)


def _eff_rebalance_cooldown_s() -> int:
    # NOTE: do NOT reference PORTFOLIO_REBALANCE_COOLDOWN_S here because that constant
    # is defined later in the file (import-time NameError). Read env directly.
    v = int(os.environ.get("PORTFOLIO_REBALANCE_COOLDOWN_S", "60"))
    if _capital_mode() == "preserve":
        try:
            v = int(float(v) * float(max(1.0, float(PORTFOLIO_PRESERVE_REBALANCE_COOLDOWN_MULT))))
        except Exception:
            pass
    return int(v)

# -----------------------------
# Stress / regime risk gate (opt-in)
# -----------------------------
# Compress exposure under elevated stress (e.g., VIX regime).
PORTFOLIO_USE_STRESS_GATE = os.environ.get("PORTFOLIO_USE_STRESS_GATE", "0") == "1"

# Trigger on VIX z-score vs trailing window (computed from prices where symbol='VIX')
PORTFOLIO_STRESS_VIX_Z_TH = float(os.environ.get("PORTFOLIO_STRESS_VIX_Z_TH", "1.25"))

# When above threshold, linearly compress down to min factor
PORTFOLIO_STRESS_MIN_FACTOR = float(os.environ.get("PORTFOLIO_STRESS_MIN_FACTOR", "0.35"))
PORTFOLIO_STRESS_Z_AT_MIN = float(os.environ.get("PORTFOLIO_STRESS_Z_AT_MIN", "3.0"))

# -----------------------------
# Social manipulation / attention gate (opt-in)
# -----------------------------
PORTFOLIO_USE_SOCIAL_GATE = os.environ.get("PORTFOLIO_USE_SOCIAL_GATE", "0") == "1"
PORTFOLIO_SOCIAL_BUCKET_SEC = int(os.environ.get("PORTFOLIO_SOCIAL_BUCKET_SEC", "300"))
PORTFOLIO_SOCIAL_MANIP_BLOCK_TH = float(os.environ.get("PORTFOLIO_SOCIAL_MANIP_BLOCK_TH", "0.85"))
PORTFOLIO_SOCIAL_ATTEN_SHOCK_TH = float(os.environ.get("PORTFOLIO_SOCIAL_ATTEN_SHOCK_TH", "0.80"))
PORTFOLIO_SOCIAL_SHOCK_FACTOR = float(os.environ.get("PORTFOLIO_SOCIAL_SHOCK_FACTOR", "0.60"))

# Optional per-symbol "vol-of-vol" compression (uses price-only proxy)
PORTFOLIO_USE_VOV_GATE = os.environ.get("PORTFOLIO_USE_VOV_GATE", "0") == "1"
PORTFOLIO_VOV_ALPHA = float(os.environ.get("PORTFOLIO_VOV_ALPHA", "6.0"))  # strength of penalty
PORTFOLIO_VOV_FLOOR = float(os.environ.get("PORTFOLIO_VOV_FLOOR", "0.0"))
PORTFOLIO_VOV_CEIL = float(os.environ.get("PORTFOLIO_VOV_CEIL", "0.020"))

# Impact-aware sizing (uses realized slippage from execution_metrics)
PORTFOLIO_IMPACT_SIZING = os.environ.get("PORTFOLIO_IMPACT_SIZING", "1") == "1"
PORTFOLIO_IMPACT_LOOKBACK_METRICS = int(os.environ.get("PORTFOLIO_IMPACT_LOOKBACK_METRICS", "5000"))
PORTFOLIO_IMPACT_BAD_BPS = float(os.environ.get("PORTFOLIO_IMPACT_BAD_BPS", "25.0"))
PORTFOLIO_IMPACT_FLOOR = float(os.environ.get("PORTFOLIO_IMPACT_FLOOR", "0.25"))

# Capital allocation optimizer (reweights desired based on expected_ret_net / expected_dd)
PORTFOLIO_ALLOC_OPT = os.environ.get("PORTFOLIO_ALLOC_OPT", "1") == "1"
PORTFOLIO_ALLOC_ALPHA = float(os.environ.get("PORTFOLIO_ALLOC_ALPHA", "1.0"))   # return weight
PORTFOLIO_ALLOC_BETA = float(os.environ.get("PORTFOLIO_ALLOC_BETA", "1.0"))     # dd penalty
PORTFOLIO_ALLOC_FLOOR = float(os.environ.get("PORTFOLIO_ALLOC_FLOOR", "0.20"))  # min factor vs original
PORTFOLIO_ALLOC_CEIL = float(os.environ.get("PORTFOLIO_ALLOC_CEIL", "2.00"))    # max factor vs original

# ------------------------------------------------------
# Capital Efficiency Native Weighting
# ------------------------------------------------------
PORTFOLIO_USE_EFFICIENCY_WEIGHTING = os.environ.get("PORTFOLIO_USE_EFFICIENCY_WEIGHTING", "1") == "1"
PORTFOLIO_EFF_ALPHA = float(os.environ.get("PORTFOLIO_EFF_ALPHA", "1.0"))
PORTFOLIO_EFF_DD_PENALTY = float(os.environ.get("PORTFOLIO_EFF_DD_PENALTY", "0.50"))
PORTFOLIO_EFF_FLOOR = float(os.environ.get("PORTFOLIO_EFF_FLOOR", "0.25"))
PORTFOLIO_EFF_CEIL = float(os.environ.get("PORTFOLIO_EFF_CEIL", "2.50"))

# Capital-at-Risk gate (tail-risk budget)
# risk_i = weight_i * expected_dd_i  (expected_dd from tradability block)
PORTFOLIO_CAR_MAX = float(os.environ.get("PORTFOLIO_CAR_MAX", "0.06"))  # max portfolio risk budget
PORTFOLIO_CAR_MAX_PER_SYMBOL = float(os.environ.get("PORTFOLIO_CAR_MAX_PER_SYMBOL", "0.03"))

# Per-symbol weight cap
PORTFOLIO_MAX_W_PER_SYMBOL = float(os.environ.get("PORTFOLIO_MAX_W_PER_SYMBOL", "0.45"))

# Score normalization (weight = gross_cap * score/score_norm)
PORTFOLIO_SCORE_NORM = float(os.environ.get("PORTFOLIO_SCORE_NORM", "3.0"))

# Minimum hold time before allowing reversal
PORTFOLIO_MIN_HOLD_S = int(os.environ.get("PORTFOLIO_MIN_HOLD_S", "1800"))  # 30 min

# Cooldown between rebalances (avoid constant churn)
PORTFOLIO_REBALANCE_COOLDOWN_S = int(os.environ.get("PORTFOLIO_REBALANCE_COOLDOWN_S", "60"))
# stale price block
PORTFOLIO_EXEC_STALE_HALF_FACTOR = float(os.environ.get("PORTFOLIO_EXEC_STALE_HALF_FACTOR", "0.50"))

# stress throttle (Market Stress Score 0..1)
PORTFOLIO_EXEC_STRESS_TH = float(os.environ.get("PORTFOLIO_EXEC_STRESS_TH", "0.75"))
PORTFOLIO_EXEC_STRESS_FACTOR = float(os.environ.get("PORTFOLIO_EXEC_STRESS_FACTOR", "0.60"))

# ------            -- ------------------------------------------------------
# Execution realism sizing (opt-in, recommended)
# - staleness per symbol (price age)
# - global stress proxy (VIX z-score if available)
# - volatility proxy (ATR% from price-only series)
# ------            -- ------------------------------------------------------
PORTFOLIO_USE_EXEC_REALISM = os.environ.get("PORTFOLIO_USE_EXEC_REALISM", "1") == "1"

# If symbol price is older than this, size -> 0 (blocks new exposure via portfolio intents)
PORTFOLIO_EXEC_MAX_PRICE_AGE_S = float(os.environ.get("PORTFOLIO_EXEC_MAX_PRICE_AGE_S", "120"))

# Stress throttle (requires VIX being present in prices as symbol="VIX")
PORTFOLIO_EXEC_VIX_Z_TH = float(os.environ.get("PORTFOLIO_EXEC_VIX_Z_TH", "2.0"))
PORTFOLIO_EXEC_VIX_FACTOR = float(os.environ.get("PORTFOLIO_EXEC_VIX_FACTOR", "0.60"))

# Volatility throttle based on ATR% (price-only proxy)
PORTFOLIO_EXEC_ATR_PCT_TH = float(os.environ.get("PORTFOLIO_EXEC_ATR_PCT_TH", "0.02"))
# Extra slippage estimate (bps) ~ atr_pct * 1e4 * multiplier (audit only; does not change expected_ret here)
PORTFOLIO_EXEC_ATR_SLIP_MULT = float(os.environ.get("PORTFOLIO_EXEC_ATR_SLIP_MULT", "0.25"))

# ------------------------------------------------------
# Execution Regime Sizing (model-aware execution layer)
# ------------------------------------------------------
PORTFOLIO_USE_EXEC_REGIME = os.environ.get("PORTFOLIO_USE_EXEC_REGIME", "1") == "1"

PORTFOLIO_EXEC_SKEW_Z_TH = float(os.environ.get("PORTFOLIO_EXEC_SKEW_Z_TH", "1.5"))
PORTFOLIO_EXEC_FLOW_Z_TH = float(os.environ.get("PORTFOLIO_EXEC_FLOW_Z_TH", "2.0"))

PORTFOLIO_EXEC_STRESS_SIZE_REDUCTION = float(os.environ.get("PORTFOLIO_EXEC_STRESS_SIZE_REDUCTION", "0.35"))
PORTFOLIO_EXEC_EARNINGS_SIZE_REDUCTION = float(os.environ.get("PORTFOLIO_EXEC_EARNINGS_SIZE_REDUCTION", "0.55"))

PORTFOLIO_EXEC_REGIME_FLOOR = float(os.environ.get("PORTFOLIO_EXEC_REGIME_FLOOR", "0.20"))

# Temporal clustering dampener (burst control)
PORTFOLIO_TD_WINDOW_S = int(os.environ.get("PORTFOLIO_TD_WINDOW_S", "1800"))  # 30 min
PORTFOLIO_TD_MAX_SIGNALS = int(os.environ.get("PORTFOLIO_TD_MAX_SIGNALS", "3"))
PORTFOLIO_TD_SCALE = float(os.environ.get("PORTFOLIO_TD_SCALE", "0.65"))  # scale once threshold exceeded

# Correlation controls (avoid redundant bets)
PORTFOLIO_CORR_PRUNE = os.environ.get("PORTFOLIO_CORR_PRUNE", "1") == "1"
PORTFOLIO_CORR_LOOKBACK = int(os.environ.get("PORTFOLIO_CORR_LOOKBACK", "240"))
PORTFOLIO_CORR_MAX = float(os.environ.get("PORTFOLIO_CORR_MAX", "0.92"))

# Correlation-aware convex optimizer (preferred over prune)
PORTFOLIO_CORR_OPT = os.environ.get("PORTFOLIO_CORR_OPT", "1") == "1"
PORTFOLIO_CORR_OPT_RIDGE = float(os.environ.get("PORTFOLIO_CORR_OPT_RIDGE", "1e-6"))
PORTFOLIO_CORR_OPT_ITERS = int(os.environ.get("PORTFOLIO_CORR_OPT_ITERS", "30"))

# Adaptive gamma by regime (LOW/MID/HIGH)
# gamma controls how strongly utility competes with risk in the convex optimizer.
PORTFOLIO_CORR_OPT_GAMMA_BASE = float(os.environ.get("PORTFOLIO_CORR_OPT_GAMMA_BASE", "1.0"))
PORTFOLIO_CORR_OPT_GAMMA_LOW = float(os.environ.get("PORTFOLIO_CORR_OPT_GAMMA_LOW", "1.20"))
PORTFOLIO_CORR_OPT_GAMMA_MID = float(os.environ.get("PORTFOLIO_CORR_OPT_GAMMA_MID", "1.00"))
PORTFOLIO_CORR_OPT_GAMMA_HIGH = float(os.environ.get("PORTFOLIO_CORR_OPT_GAMMA_HIGH", "0.70"))

# Regime sizing anchor (used by dev_core.regime_size)
PORTFOLIO_REGIME_ANCHOR = os.environ.get("PORTFOLIO_REGIME_ANCHOR", "SPY").strip().upper()

# Price freshness (symbol-level data quality gate)
PORTFOLIO_MAX_PRICE_STALE_S = int(os.environ.get("PORTFOLIO_MAX_PRICE_STALE_S", "180"))  # 3 min

# Optional per-symbol caps via JSON, e.g.:
#   set PORTFOLIO_SYMBOL_CAPS={"SPY":0.5,"BTC":0.35,"OIL":0.35}
_SYMBOL_CAPS_RAW = os.environ.get("PORTFOLIO_SYMBOL_CAPS", "").strip()
PORTFOLIO_SYMBOL_CAPS = {}
if _SYMBOL_CAPS_RAW:
    try:
        PORTFOLIO_SYMBOL_CAPS = json.loads(_SYMBOL_CAPS_RAW)
        if not isinstance(PORTFOLIO_SYMBOL_CAPS, dict):
            PORTFOLIO_SYMBOL_CAPS = {}
    except Exception:
        PORTFOLIO_SYMBOL_CAPS = {}

# -----------------------------
# DB schema (owned by this module)
# -----------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_state (
  symbol TEXT PRIMARY KEY,
  side TEXT NOT NULL,              -- LONG / SHORT / FLAT
  weight REAL NOT NULL,            -- 0..1 (fraction of capital)
  opened_ts_ms INTEGER NOT NULL,
  updated_ts_ms INTEGER NOT NULL,
  source_alert_id INTEGER,
  explain_json TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  action TEXT NOT NULL,            -- OPEN / INCREASE / DECREASE / CLOSE / REVERSE / HOLD
  from_side TEXT NOT NULL,
  to_side TEXT NOT NULL,
  from_weight REAL NOT NULL,
  to_weight REAL NOT NULL,
  delta_weight REAL NOT NULL,
  source_alert_id INTEGER,
  explain_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_portfolio_orders_ts
  ON portfolio_orders(ts_ms);

CREATE TABLE IF NOT EXISTS portfolio_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""
# =========================
# SECTION 2 / ~200 lines
# =========================

def init_portfolio_db():
    con = connect()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def _get_meta(con, key: str) -> Optional[str]:
    row = con.execute("SELECT value FROM portfolio_meta WHERE key=?", (str(key),)).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def _set_meta(con, key: str, value: str) -> None:
    con.execute(
        """
        INSERT INTO portfolio_meta(key, value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(key), str(value)),
    )


# Alias for consistency with other modules
_put_meta = _set_meta


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(float(lo), min(float(hi), float(x)))


def _stdev(xs):
    xs = [float(x) for x in (xs or [])]
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    v = sum((x - m) * (x - m) for x in xs) / (n - 1)
    s = (v ** 0.5) if v > 0 else 0.0
    return float(s)


def _vix_stress(con, lookback: int = 180) -> dict:
    """
    Reads VIX from prices table where symbol='VIX'.
    Returns:
      level, z (zscore vs trailing window), change_1 (last - prev)
    Fail-soft: returns zeros if unavailable.
    """
    try:
        rows = con.execute(
            """
            SELECT price
            FROM prices
            WHERE symbol='VIX'
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (int(lookback),),
        ).fetchall()
        v = [float(r[0]) for r in (rows or []) if r and r[0] is not None]
        v.reverse()
        if len(v) < 5:
            return {"level": 0.0, "z": 0.0, "change_1": 0.0}

        level = float(v[-1])
        change_1 = float(level - float(v[-2]))

        # zscore vs trailing (use last up to 60 points if available)
        w = v[-60:] if len(v) >= 60 else v
        s = _stdev(w)
        if s is None or s <= 1e-12:
            z = 0.0
        else:
            m = sum(w) / len(w)
            z = (level - float(m)) / float(s)
            if z != z:  # NaN
                z = 0.0

        return {"level": float(level), "z": float(z), "change_1": float(change_1)}
    except Exception:
        return {"level": 0.0, "z": 0.0, "change_1": 0.0}


def _stress_factor_from_vix_z(z: float) -> float:
    """
    Piecewise-linear compression:
      z <= TH        => 1.0
      z >= Z_AT_MIN  => MIN_FACTOR
      else linear in-between
    """
    try:
        z = float(z)
    except Exception:
        z = 0.0

    th = float(PORTFOLIO_STRESS_VIX_Z_TH)
    zmin = float(PORTFOLIO_STRESS_Z_AT_MIN)
    fmin = float(PORTFOLIO_STRESS_MIN_FACTOR)

    if z <= th:
        return 1.0
    if z >= zmin:
        return float(_clamp(fmin, 0.0, 1.0))

    # interpolate from 1.0 at th down to fmin at zmin
    t = (z - th) / max(1e-9, (zmin - th))
    f = 1.0 + (float(_clamp(fmin, 0.0, 1.0)) - 1.0) * float(_clamp(t, 0.0, 1.0))
    return float(_clamp(f, 0.0, 1.0))


def _symbol_cap(symbol: str) -> float:
    # symbol-specific cap if provided, else global cap
    if symbol in PORTFOLIO_SYMBOL_CAPS:
        try:
            return float(PORTFOLIO_SYMBOL_CAPS[symbol])
        except Exception:
            return float(PORTFOLIO_MAX_W_PER_SYMBOL)
    return float(PORTFOLIO_MAX_W_PER_SYMBOL)


def _last_price_age_s(con, symbol: str, now_ms: int) -> Optional[float]:
    try:
        r = con.execute(
            "SELECT ts_ms FROM prices WHERE symbol=? ORDER BY ts_ms DESC LIMIT 1",
            (str(symbol),),
        ).fetchone()
        if not r or r[0] is None:
            return None
        age_s = (int(now_ms) - int(r[0])) / 1000.0
        if not math.isfinite(age_s):
            return None
        return float(max(0.0, age_s))
    except Exception:
        return None


def _exec_realism_factor(con, symbol: str, now_ms: int) -> Tuple[float, Dict[str, float]]:
    """
    Returns (factor, meta). factor in [0,1].
    Hard block: stale prices beyond PORTFOLIO_EXEC_MAX_PRICE_AGE_S.
    Soft throttle: Market Stress Score above threshold.
    """
    meta: Dict[str, float] = {
        "staleness_sec": 0.0,
        "stress_score": 0.0,
    }

    f = 1.0

    # staleness (hard + soft)
    age_s = _last_price_age_s(con, symbol, int(now_ms))
    if age_s is None:
        age_s = 0.0
    meta["staleness_sec"] = float(age_s)

    max_age = float(PORTFOLIO_EXEC_MAX_PRICE_AGE_S)
    if max_age > 0 and age_s > max_age:
        return 0.0, meta
    if max_age > 0 and age_s > (0.5 * max_age):
        f *= float(PORTFOLIO_EXEC_STALE_HALF_FACTOR)

    # global stress (read-only)
    try:
        from dev_core.market_stress import get_market_stress_snapshot

        ms = get_market_stress_snapshot(con=con, ts_ms=int(now_ms)) or {}
        stress = float(ms.get("stress_score", 0.0))
        if not math.isfinite(stress):
            stress = 0.0
    except Exception:
        stress = 0.0
    meta["stress_score"] = float(stress)

    if stress >= float(PORTFOLIO_EXEC_STRESS_TH):
        f *= float(PORTFOLIO_EXEC_STRESS_FACTOR)

    if not math.isfinite(f):
        f = 1.0
    f = float(max(0.0, min(1.0, f)))
    return f, meta


def _execution_realism_factor(con, symbol: str, now_ms: int) -> Tuple[float, Dict[str, float]]:
    """
    Returns (factor, meta) where factor in [0,1].
    Fail-soft: if we can't compute anything, returns (1.0, meta with zeros).
    Hard block: stale prices beyond PORTFOLIO_EXEC_MAX_PRICE_AGE_S => factor=0.
    """
    meta: Dict[str, float] = {
        "staleness_sec": 0.0,
        "stress_vix_z_60": 0.0,
        "atr_pct": 0.0,
        "slippage_bps_est": 0.0,
    }

    f = 1.0

    # 1) Per-symbol staleness (hard safety + soft throttle)
    age_s = _last_price_age_s(con, symbol, int(now_ms))
    if age_s is None:
        # Unknown staleness => do not hard-block here (kill_switch may still block execution).
        age_s = 0.0
    meta["staleness_sec"] = float(age_s)

    max_age = float(PORTFOLIO_EXEC_MAX_PRICE_AGE_S)
    if max_age > 0 and age_s > max_age:
        return 0.0, meta
    if max_age > 0 and age_s > (0.5 * max_age):
        f *= float(PORTFOLIO_EXEC_STALE_HALF_FACTOR)

    # 2) Optional market stress + volatility proxies via tech_indicators (price-only)
    try:
        from dev_core.tech_indicators import compute_tech_features

        tf = compute_tech_features(str(symbol), int(now_ms)) or {}
    except Exception:
        tf = {}

    try:
        vix_z = float(tf.get("stress_vix_z_60", 0.0))
        if not math.isfinite(vix_z):
            vix_z = 0.0
    except Exception:
        vix_z = 0.0
    meta["stress_vix_z_60"] = float(vix_z)

    try:
        atr_pct = float(tf.get("atr_pct", 0.0))
        if not math.isfinite(atr_pct):
            atr_pct = 0.0
    except Exception:
        atr_pct = 0.0
    meta["atr_pct"] = float(atr_pct)

    # Stress throttle
    if vix_z > float(PORTFOLIO_EXEC_VIX_Z_TH):
        f *= float(PORTFOLIO_EXEC_VIX_FACTOR)

    # Volatility throttle: if atr_pct above threshold, scale down ~ (th/atr_pct)
    atr_th = float(PORTFOLIO_EXEC_ATR_PCT_TH)
    if atr_th > 0 and atr_pct > atr_th:
        f *= float(max(0.0, min(1.0, atr_th / max(1e-12, atr_pct))))

    # Slippage estimate (audit / explainability)
    try:
        slip = float(atr_pct) * 1e4 * float(PORTFOLIO_EXEC_ATR_SLIP_MULT)
        if not math.isfinite(slip):
            slip = 0.0
    except Exception:
        slip = 0.0
    meta["slippage_bps_est"] = float(max(0.0, slip))

    # Clamp
    if not math.isfinite(f):
        f = 1.0
    f = float(max(0.0, min(1.0, f)))
    return f, meta


# How strongly novelty boosts a signal:
# final_score = base_score * (1 + NOVELTY_ALPHA * novelty)
PORTFOLIO_NOVELTY_ALPHA = float(os.environ.get("PORTFOLIO_NOVELTY_ALPHA", "0.50"))
# Exploration controls: cap weight for symbols with low history
PORTFOLIO_EXPLORE_MIN_LABELS = int(os.environ.get("PORTFOLIO_EXPLORE_MIN_LABELS", "25"))
PORTFOLIO_EXPLORE_MAX_W = float(os.environ.get("PORTFOLIO_EXPLORE_MAX_W", "0.05"))  # 5% cap for "new" symbols
# =========================
# SECTION 3 / ~200 lines
# =========================

def _novelty_from_explain(explain_json: str) -> float:
    try:
        x = json.loads(explain_json or "{}")
        meta = x.get("event_meta") if isinstance(x, dict) else None
        if not isinstance(meta, dict):
            return 0.0
        v = float(meta.get("novelty", 0.0))
        if v != v:
            return 0.0
        return max(0.0, min(1.0, v))
    except Exception:
        return 0.0


def _score_from_alert(z: float, conf: float, severity: str, explain_json: str) -> float:
    # core score: abs(z)*conf
    # small severity bump (already gated by rules)
    s = abs(float(z)) * float(conf)
    sev = (severity or "").upper()
    if sev == "CRIT":
        s *= 1.15
    elif sev == "HIGH":
        s *= 1.08

    novelty = _novelty_from_explain(explain_json)
    s *= (1.0 + float(PORTFOLIO_NOVELTY_ALPHA) * float(novelty))
    return float(s)


def _tradability_from_explain(explain_json: str) -> Dict[str, float]:
    try:
        ex = json.loads(explain_json or "{}")
        tr = ex.get("tradability") or {}
        return {
            "expected_ret_net": float(tr.get("expected_ret_net", 0.0)),
            "p_win": float(tr.get("p_win", 0.5)),
            "expected_dd": float(tr.get("expected_dd", 0.0)),
        }
    except Exception:
        return {
            "expected_ret_net": 0.0,
            "p_win": 0.5,
            "expected_dd": 0.0,
        }


def _latest_price_ts_ms(con, symbol: str) -> Optional[int]:
    """
    Best-effort lookup of latest price timestamp for symbol.
    Assumes `prices(symbol, ts_ms, px)` exists (used elsewhere in your codebase).
    """
    try:
        row = con.execute(
            """
            SELECT ts_ms
            FROM prices
            WHERE symbol=?
            ORDER BY ts_ms DESC
            LIMIT 1
            """,
            (str(symbol),),
        ).fetchone()
        if not row:
            return None
        return int(row[0])
    except Exception:
        return None


def _is_price_fresh(con, symbol: str, now_ms: int) -> bool:
    ts = _latest_price_ts_ms(con, symbol)
    if ts is None:
        return False
    age_s = max(0.0, (int(now_ms) - int(ts)) / 1000.0)
    return age_s <= float(PORTFOLIO_MAX_PRICE_STALE_S)


def _desired_weight(score: float, symbol: str) -> float:
    # weight proportional to score, capped by symbol cap and gross cap later
    w = (float(score) / float(PORTFOLIO_SCORE_NORM)) * float(PORTFOLIO_GROSS_CAP)
    w = _clamp(w, 0.0, _symbol_cap(symbol))
    return float(w)


def _load_recent_alert_candidates(con, lookback_s: int) -> List[Dict]:
    now_ms = _now_ms()
    cutoff_ms = int(now_ms) - int(lookback_s) * 1000

    # Dynamic universe filter: only consider ACTIVE + WATCH symbols
    try:
        allowed = set(get_active_symbols(con, limit=int(os.environ.get("PORTFOLIO_SYMBOL_LIMIT", "5000"))))
    except Exception:
        allowed = set()

    rows = con.execute(
        """
        SELECT id, ts_ms, symbol, horizon_s, expected_z, confidence, severity, event_title, explain_json
        FROM alerts
        WHERE ts_ms >= ?
        ORDER BY ts_ms DESC
        """,
        (int(cutoff_ms),),
    ).fetchall()

    out = []
    for r in rows or []:
        try:
            sym = str(r[2])

            if allowed and sym not in allowed:
                continue

            # Data quality gate: skip symbols with stale/missing price
            if not _is_price_fresh(con, sym, int(now_ms)):
                continue

            out.append(
                {
                    "id": int(r[0]),
                    "ts_ms": int(r[1]),
                    "symbol": sym,
                    "horizon_s": int(r[3]),
                    "expected_z": float(r[4]),
                    "confidence": float(r[5]),
                    "severity": str(r[6] or ""),
                    "event_title": str(r[7] or ""),
                    "explain_json": str(r[8] or "{}"),
                }
            )
        except Exception:
            continue
    return out


def _pick_best_per_symbol(alerts: List[Dict]) -> Dict[str, Dict]:
    """
    Choose best candidate per symbol across horizons.
    Criteria: max score (abs(z)*conf with severity bump).
    """
    best = {}
    for a in alerts:
        sym = a["symbol"]
        z = float(a["expected_z"])
        conf = float(a["confidence"])
        if conf < _eff_min_conf():
            continue
        if abs(z) < _eff_min_abs_z():
            continue

        # Base score from signal strength
        base_score = _score_from_alert(z, conf, a.get("severity"), a.get("explain_json", "{}"))

        # Tradability adjustment (from explain_json)
        tr = _tradability_from_explain(a.get("explain_json", "{}"))
        net = float(tr.get("expected_ret_net", 0.0))
        pwin = float(tr.get("p_win", 0.5))
        dd = float(tr.get("expected_dd", 0.0))

        # Penalize negative expectancy, reward positive
        tradability_mult = 1.0
        if net < 0.0:
            tradability_mult *= 0.5
        else:
            tradability_mult *= (1.0 + min(0.5, net * 10.0))

        # Modest p(win) influence (kept conservative)
        tradability_mult *= (0.75 + 0.5 * max(0.0, min(1.0, pwin)))

        # Drawdown penalty (soft)
        tradability_mult *= (1.0 / (1.0 + dd * 10.0))

        score = base_score * tradability_mult
        cur = best.get(sym)
        if (cur is None) or (score > float(cur.get("_score", 0.0))):
            b = dict(a)
            b["_score"] = float(score)
            best[sym] = b
    return best


def _apply_temporal_dampener(con, desired: Dict[str, Dict], now_ms: int) -> Dict[str, Dict]:
    """
    If a symbol has too many recent alerts in TD window, scale its weight down.
    Uses alerts table (already exists).
    """
    if not desired:
        return desired

    cutoff = int(now_ms) - int(PORTFOLIO_TD_WINDOW_S) * 1000
    for sym in list(desired.keys()):
        try:
            row = con.execute(
                """
                SELECT COUNT(1)
                FROM alerts
                WHERE symbol=? AND ts_ms >= ?
                """,
                (str(sym), int(cutoff)),
            ).fetchone()
            n = int(row[0] or 0) if row else 0
        except Exception:
            n = 0

        if n > int(PORTFOLIO_TD_MAX_SIGNALS):
            try:
                desired[sym]["weight"] = float(desired[sym].get("weight", 0.0) or 0.0) * float(PORTFOLIO_TD_SCALE)
                desired[sym].setdefault("reason", {})
                desired[sym]["reason"]["temporal_dampener"] = True
                desired[sym]["reason"]["td_n"] = int(n)
                desired[sym]["reason"]["td_window_s"] = int(PORTFOLIO_TD_WINDOW_S)
                desired[sym]["reason"]["td_scale"] = float(PORTFOLIO_TD_SCALE)
            except Exception:
                pass

    # renormalize gross after dampener
    grossT = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
    if grossT > float(PORTFOLIO_GROSS_CAP) and grossT > 1e-9:
        scaleT = float(PORTFOLIO_GROSS_CAP) / float(grossT)
        for sym in list(desired.keys()):
            nw = float(desired[sym].get("weight", 0.0) or 0.0) * float(scaleT)
            desired[sym]["weight"] = nw if math.isfinite(nw) else 0.0

    return desired


def _load_avg_slippage_bps_by_symbol(con, limit_metrics: int) -> Dict[str, float]:
    """
    Reads execution_metrics to compute avg slippage bps per symbol.
    Uses latest available metrics (across timestamps).
    """
    out: Dict[str, float] = {}
    try:
        rows = con.execute(
            """
            SELECT symbol, AVG(COALESCE(slippage_bps,0.0)) AS avg_slip
            FROM (
              SELECT symbol, slippage_bps
              FROM execution_metrics
              ORDER BY ts_ms DESC
              LIMIT ?
            )
            GROUP BY symbol
            """,
            (int(max(1, min(100000, int(limit_metrics)))),),
        ).fetchall()

        for sym, avg_slip in rows or []:
            try:
                s = str(sym).upper().strip()
                if not s:
                    continue
                out[s] = float(avg_slip or 0.0)
            except Exception:
                continue
    except Exception:
        return {}
    return out


def _apply_impact_aware_sizing(con, desired: Dict[str, Dict]) -> Dict[str, Dict]:
    """
    Scales weights down for symbols with poor realized slippage.
    factor = clamp(1 - (abs(slip_bps)/IMPACT_BAD_BPS - 1), floor..1)
    """
    if not desired or not PORTFOLIO_IMPACT_SIZING:
        return desired

    slip = _load_avg_slippage_bps_by_symbol(con, int(PORTFOLIO_IMPACT_LOOKBACK_METRICS))
    if not slip:
        return desired

    bad = float(PORTFOLIO_IMPACT_BAD_BPS)
    floor = float(PORTFOLIO_IMPACT_FLOOR)

    for sym in list(desired.keys()):
        s = str(sym).upper().strip()
        sbps = float(slip.get(s, 0.0))
        a = abs(sbps)
        if bad <= 1e-9:
            continue
        if a <= bad:
            # no penalty
            desired[sym].setdefault("reason", {})
            desired[sym]["reason"]["impact_slip_bps"] = float(sbps)
            desired[sym]["reason"]["impact_factor"] = 1.0
            continue

        # if slippage is 2x bad, factor ~ 0.0 -> floored
        raw = 1.0 - ((a / bad) - 1.0)
        f = max(float(floor), min(1.0, float(raw)))

        try:
            desired[sym]["weight"] = float(desired[sym].get("weight", 0.0) or 0.0) * float(f)
            desired[sym].setdefault("reason", {})
            desired[sym]["reason"]["impact_slip_bps"] = float(sbps)
            desired[sym]["reason"]["impact_factor"] = float(f)
        except Exception:
            pass

    gross = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
    eff_cap = float(_eff_gross_cap())
    if gross > float(eff_cap) and gross > 1e-9:
        sc = float(eff_cap) / float(gross)
        for sym in list(desired.keys()):
            try:
                nw = float(desired[sym].get("weight", 0.0) or 0.0) * float(sc)
                desired[sym]["weight"] = float(nw) if math.isfinite(nw) else 0.0
            except Exception:
                desired[sym]["weight"] = 0.0

    return desired
# =========================
# SECTION 4 / ~200 lines
# =========================

def _optimize_capital_allocation(con, desired: Dict[str, Dict]) -> Dict[str, Dict]:
    """
    Reweights desired weights using:
      utility ~ (expected_ret_net^alpha) / (expected_dd^beta + eps)
    Then scales each symbol relative to its original weight and clamps factor.
    """
    if not desired or not PORTFOLIO_ALLOC_OPT:
        return desired

    alpha = float(PORTFOLIO_ALLOC_ALPHA)
    beta = float(PORTFOLIO_ALLOC_BETA)
    fmin = float(PORTFOLIO_ALLOC_FLOOR)
    fmax = float(PORTFOLIO_ALLOC_CEIL)

    # compute utilities from tradability
    util = {}
    now_ms = _now_ms()

    for sym, tgt in desired.items():
        tr = _tradability_from_explain(tgt.get("explain_json", "{}"))
        net = float(tr.get("expected_ret_net", 0.0) or 0.0)
        dd = float(tr.get("expected_dd", 0.0) or 0.0)
        dd = max(0.0, min(1.0, dd))

        # conservative: ignore negative expectancy in optimizer
        netp = max(0.0, net)

        # ----------------------------
        # Execution Regime Alpha Boost
        # ----------------------------
        regime_mult = 1.0

        if PORTFOLIO_USE_EXEC_REGIME:
            try:
                skew_z = float(_get_factor_feature_asof(con, "options.skew_25d_z", int(now_ms)))
                flow_z = float(_get_factor_feature_asof(con, "flows.index_constituent_imbalance_z", int(now_ms)))
            except Exception:
                skew_z = 0.0
                flow_z = 0.0

            stress_mag = max(
                0.0,
                max(
                    abs(skew_z) - float(PORTFOLIO_EXEC_SKEW_Z_TH),
                    abs(flow_z) - float(PORTFOLIO_EXEC_FLOW_Z_TH),
                ),
            )

            if stress_mag > 0.0:
                regime_mult *= float(
                    _clamp(
                        1.0 - (stress_mag * float(PORTFOLIO_EXEC_STRESS_SIZE_REDUCTION)),
                        float(PORTFOLIO_EXEC_REGIME_FLOOR),
                        1.0,
                    )
                )

            # earnings proximity penalty
            try:
                row_e = con.execute(
                    """
                    SELECT earnings_date
                    FROM earnings_calendar
                    WHERE symbol=?
                    ORDER BY ABS(julianday(earnings_date) - julianday(date('now'))) ASC
                    LIMIT 1
                    """,
                    (str(sym),),
                ).fetchone()

                if row_e and row_e[0]:
                    jd = con.execute(
                        "SELECT (julianday(?) - julianday(date('now')))",
                        (str(row_e[0]),),
                    ).fetchone()
                    if jd and jd[0] is not None:
                        days = float(jd[0])
                        decay = math.exp(-abs(days) / 5.0)
                        earnings_pen = float(
                            _clamp(
                                1.0 - (decay * float(PORTFOLIO_EXEC_EARNINGS_SIZE_REDUCTION)),
                                float(PORTFOLIO_EXEC_REGIME_FLOOR),
                                1.0,
                            )
                        )
                        regime_mult *= earnings_pen
            except Exception:
                pass

        u = ((netp ** max(0.0, alpha)) / ((dd ** max(0.0, beta)) + 1e-6)) * float(regime_mult)

        util[str(sym)] = float(u)

        desired[sym].setdefault("reason", {})
        desired[sym]["reason"]["optimizer_regime_mult"] = float(regime_mult)

    # if all utilities are zero, do nothing
    tot_u = sum(float(u) for u in util.values())
    if tot_u <= 1e-12:
        return desired

    # apply multiplicative factor relative to original weights
    for sym in list(desired.keys()):
        u = float(util.get(sym, 0.0))
        # normalized utility share
        share = u / tot_u if tot_u > 0 else 0.0

        w0 = float(desired[sym].get("weight", 0.0) or 0.0)
        if w0 <= 0:
            continue

        # target weight proportional to share, but keep within factor bounds vs original
        wt = float(PORTFOLIO_GROSS_CAP) * float(share)
        factor = wt / max(1e-9, w0)
        factor = max(fmin, min(fmax, float(factor)))

        desired[sym]["weight"] = float(w0) * float(factor)
        desired[sym].setdefault("reason", {})
        desired[sym]["reason"]["alloc_util"] = float(u)
        desired[sym]["reason"]["alloc_share"] = float(share)
        desired[sym]["reason"]["alloc_factor"] = float(factor)

    # renormalize gross
    gross = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
    eff_cap = float(_eff_gross_cap())
    if gross > float(eff_cap) and gross > 1e-9:
        sc = float(eff_cap) / float(gross)
        for sym in list(desired.keys()):
            try:
                nw = float(desired[sym].get("weight", 0.0) or 0.0) * float(sc)
                desired[sym]["weight"] = float(nw) if math.isfinite(nw) else 0.0
            except Exception:
                desired[sym]["weight"] = 0.0

    return desired


def _load_live_strategies(con) -> List[str]:
    """
    Returns all strategies currently in 'live' stage.
    """
    try:
        rows = con.execute(
            """
            SELECT strategy_name
            FROM strategy_registry
            WHERE stage='live'
            """
        ).fetchall()
        return [str(r[0]) for r in rows or [] if r and r[0]]
    except Exception:
        return []


def _load_strategy_efficiency(con) -> Dict[str, Dict[str, float]]:
    """
    Loads latest capital-efficiency aggregates from strategy_metrics (window_days=0).
    Returns:
        {strategy_name: {
            "efficiency_score": float,
            "return_per_risk_unit": float,
            "drawdown_contribution": float
        }}
    """
    out: Dict[str, Dict[str, float]] = {}
    try:
        rows = con.execute(
            """
            SELECT strategy_name, metrics_json
            FROM strategy_metrics
            WHERE window_days=0
            """
        ).fetchall()

        for name, mj in rows or []:
            try:
                m = json.loads(mj or "{}")
                out[str(name)] = {
                    "efficiency_score": float(m.get("efficiency_score", 0.0) or 0.0),
                    "return_per_risk_unit": float(m.get("return_per_risk_unit", 0.0) or 0.0),
                    "drawdown_contribution": float(m.get("drawdown_contribution", 0.0) or 0.0),
                }
            except Exception:
                continue
    except Exception:
        pass
    return out


def _load_state(con) -> Dict[str, Dict]:
    rows = con.execute(
        """
        SELECT symbol, side, weight, opened_ts_ms, updated_ts_ms, source_alert_id, explain_json
        FROM portfolio_state
        """
    ).fetchall()

    out = {}
    for r in rows or []:
        out[str(r[0])] = {
            "symbol": str(r[0]),
            "side": str(r[1]),
            "weight": float(r[2]),
            "opened_ts_ms": int(r[3]),
            "updated_ts_ms": int(r[4]),
            "source_alert_id": (int(r[5]) if r[5] is not None else None),
            "explain_json": str(r[6] or "{}"),
        }
    return out


def _write_state_row(
    con,
    sym: str,
    side: str,
    weight: float,
    opened_ts_ms: int,
    updated_ts_ms: int,
    source_alert_id: Optional[int],
    explain_json: str,
) -> None:
    con.execute(
        """
        INSERT INTO portfolio_state(symbol, side, weight, opened_ts_ms, updated_ts_ms, source_alert_id, explain_json)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
          side=excluded.side,
          weight=excluded.weight,
          opened_ts_ms=excluded.opened_ts_ms,
          updated_ts_ms=excluded.updated_ts_ms,
          source_alert_id=excluded.source_alert_id,
          explain_json=excluded.explain_json
        """,
        (
            str(sym),
            str(side),
            float(weight),
            int(opened_ts_ms),
            int(updated_ts_ms),
            int(source_alert_id) if source_alert_id is not None else None,
            str(explain_json or "{}"),
        ),
    )
# =========================
# SECTION 5 / ~200 lines
# =========================

def _apply_capital_at_risk_gate(desired: Dict[str, Dict]) -> Tuple[Dict[str, Dict], Dict]:
    """
    Enforce portfolio and per-symbol tail-risk budgets using expected_dd from tradability.
    risk_i = |w_i| * expected_dd_i
    """
    meta = {
        "car_enabled": True,
        "car_max": float(PORTFOLIO_CAR_MAX),
        "car_max_per_symbol": float(PORTFOLIO_CAR_MAX_PER_SYMBOL),
    }
    if not desired:
        meta["car_scaled"] = False
        return desired, meta

    # Compute per-symbol expected_dd (fallback to 0)
    risks = {}
    total_risk = 0.0

    for sym, tgt in desired.items():
        w = abs(float(tgt.get("weight", 0.0) or 0.0))
        tr = _tradability_from_explain(tgt.get("explain_json", "{}"))
        dd = float(tr.get("expected_dd", 0.0) or 0.0)
        dd = max(0.0, min(1.0, dd))

        # Per-symbol cap first
        max_w = None
        if float(PORTFOLIO_CAR_MAX_PER_SYMBOL) > 0.0:
            denom = dd if dd > 0 else 1.0
            max_w = float(PORTFOLIO_CAR_MAX_PER_SYMBOL) / max(1e-9, denom)

        if max_w is not None and w > max_w:
            new_w = float(max_w)
            if str(tgt.get("side")).upper() == "SHORT":
                tgt["weight"] = -float(new_w)
            else:
                tgt["weight"] = float(new_w)

            tgt.setdefault("reason", {})
            tgt["reason"]["car_symbol_cap"] = True
            tgt["reason"]["car_expected_dd"] = float(dd)
            tgt["reason"]["car_symbol_max_w"] = float(max_w)

        w2 = abs(float(tgt.get("weight", 0.0) or 0.0))
        r = w2 * dd
        risks[sym] = {"w": w2, "expected_dd": dd, "risk": r}
        total_risk += float(r)

    meta["car_total_risk_before"] = float(total_risk)

    # Portfolio cap via scaling if needed
    if float(PORTFOLIO_CAR_MAX) > 0.0 and total_risk > float(PORTFOLIO_CAR_MAX) and total_risk > 1e-9:
        scale = float(PORTFOLIO_CAR_MAX) / float(total_risk)
        for sym in list(desired.keys()):
            try:
                desired[sym]["weight"] = float(desired[sym].get("weight", 0.0) or 0.0) * float(scale)
                desired[sym].setdefault("reason", {})
                desired[sym]["reason"]["car_scale"] = float(scale)
            except Exception:
                pass
        meta["car_scaled"] = True
        meta["car_scale"] = float(scale)
    else:
        meta["car_scaled"] = False

    # Renormalize gross after CAR scaling (safety)
    grossC = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
    if grossC > float(PORTFOLIO_GROSS_CAP) and grossC > 1e-9:
        scaleC = float(PORTFOLIO_GROSS_CAP) / float(grossC)
        for sym in list(desired.keys()):
            w0 = float(desired[sym].get("weight", 0.0) or 0.0)
            sign = -1.0 if w0 < 0 else 1.0
            desired[sym]["weight"] = sign * abs(float(w0) * float(scaleC))
        meta["gross_renorm_after_car"] = True
        meta["gross_scale_after_car"] = float(scaleC)

    meta["car_by_symbol"] = risks
    return desired, meta


def _emit_order(
    con,
    sym: str,
    action: str,
    from_side: str,
    to_side: str,
    from_w: float,
    to_w: float,
    source_alert_id: Optional[int],
    explain: Dict,
) -> None:
    ts_ms = _now_ms()

    ex = dict(explain or {})
    ex.setdefault("execution", {})
    ex["execution"]["intent_only"] = True
    ex["execution"]["component"] = "portfolio"

    con.execute(
        """
        INSERT INTO portfolio_orders(
          ts_ms, symbol, action, from_side, to_side, from_weight, to_weight, delta_weight, source_alert_id, explain_json
        )
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            int(ts_ms),
            str(sym),
            str(action),
            str(from_side),
            str(to_side),
            float(from_w),
            float(to_w),
            float(to_w - from_w),
            int(source_alert_id) if source_alert_id is not None else None,
            json.dumps(ex, ensure_ascii=False),
        ),
    )


def compute_rebalance() -> Dict:
    """
    Main entry point for portfolio rebalance.
    """
    init_portfolio_db()
    con = connect()
    try:
        # cooldown guard
        last = _get_meta(con, "last_rebalance_ts_ms")
        now_ms = _now_ms()

        last_exec = _get_meta(con, "last_rebalance_exec_id")
        if last_exec == str(now_ms):
            return {"ok": False, "error": "duplicate rebalance execution"}

        _set_meta(con, "last_rebalance_exec_id", str(now_ms))

        if last:
            try:
                last_ms = int(last)
                eff_cd = int(_eff_rebalance_cooldown_s())
                if (now_ms - last_ms) < int(eff_cd) * 1000:
                    return {
                        "ok": False,
                        "error": "rebalance cooldown active",
                        "cooldown_s": int(eff_cd),
                    }
            except Exception:
                pass

        # capital guard (hard stop) — portfolio is intent-only, but still should not churn state when halted
        try:
            from dev_core.capital_guard import trading_allowed

            if not trading_allowed(con):
                return {"ok": False, "error": "trading halted by capital guard"}
        except Exception as e:
            return {"ok": False, "error": f"capital_guard check failed: {e}"}

        # read alerts + state
        alerts = _load_recent_alert_candidates(con, PORTFOLIO_LOOKBACK_S)
        state = _load_state(con)

        # ---            -- ------------------------------------------------------
        # Build compact selector / RL feature snapshot (B2/C2)
        # ---            -- ------------------------------------------------------
        best_for_features = _pick_best_per_symbol(alerts)
        best_vals = list(best_for_features.values())

        avg_conf = 0.0
        avg_abs_z = 0.0
        if best_vals:
            try:
                avg_conf = sum(float(a.get("confidence", 0.0)) for a in best_vals) / len(best_vals)
                avg_abs_z = sum(abs(float(a.get("expected_z", 0.0))) for a in best_vals) / len(best_vals)
            except Exception:
                avg_conf = 0.0
                avg_abs_z = 0.0

        prev_drawdown = 0.0
        try:
            # Optional: populated later by backtest / RL logging
            prev_drawdown = float(_get_meta(con, "last_drawdown") or 0.0)
        except Exception:
            prev_drawdown = 0.0

        features = {
            "n_candidates": int(len(best_vals)),
            "avg_conf": float(avg_conf),
            "avg_abs_z": float(avg_abs_z),
            "prev_drawdown": float(prev_drawdown),
        }

        # ------------------------------------------------------
        # Multi-Strategy Capital Competition
        # ------------------------------------------------------
        live_strategies = _load_live_strategies(con)
        eff_map = _load_strategy_efficiency(con)

        strategy_targets: Dict[str, Dict] = {}
        total_eff = 0.0

        strat = None  # preserve original "last loaded strat" behavior for later get_regime_profile usage

        for sname in live_strategies:
            try:
                strat = load_strategy_module(str(sname))
                d = strat.build_desired(alerts=alerts, now_ms=int(now_ms)) or {}
                strategy_targets[str(sname)] = d

                eff = float((eff_map.get(str(sname)) or {}).get("efficiency_score", 0.0) or 0.0)
                eff = max(0.0, eff)
                total_eff += eff
            except Exception:
                continue

        # If no efficiency available, equal weight fallback
        if total_eff <= 1e-9:
            total_eff = float(len(strategy_targets) or 1)

        # Merge with efficiency-weighted capital share
        desired: Dict[str, Dict] = {}

        for sname, targets in strategy_targets.items():
            eff = float((eff_map.get(str(sname)) or {}).get("efficiency_score", 0.0) or 0.0)
            eff = max(0.0, eff)
            share = eff / total_eff if total_eff > 0 else 0.0

            for sym, tgt in (targets or {}).items():
                try:
                    w = float((tgt or {}).get("weight", 0.0) or 0.0)
                    w = float(w) * float(share)

                    cur = desired.get(sym)
                    if cur is None:
                        desired[sym] = dict(tgt)
                        desired[sym]["weight"] = float(w)
                        desired[sym].setdefault("reason", {})
                        desired[sym]["reason"]["strategy"] = str(sname)
                        desired[sym]["reason"]["eff_share"] = float(share)
                    else:
                        # combine weights from multiple strategies
                        cur_w = float(cur.get("weight", 0.0) or 0.0)
                        desired[sym]["weight"] = float(cur_w + w)
                        desired[sym].setdefault("reason", {})
                        desired[sym]["reason"]["multi_strategy"] = True
                except Exception:
                    continue

        # defensive normalization (strategy modules are pluggable)
        norm: Dict[str, Dict] = {}
        for sym, tgt in (desired or {}).items():
            try:
                s = str(sym)
                side = str((tgt or {}).get("side", "FLAT")).upper()
                if side not in ("LONG", "SHORT", "FLAT"):
                    side = "FLAT"

                w = float((tgt or {}).get("weight", 0.0))
                if not (w == w):  # NaN
                    w = 0.0

                # if FLAT, force weight=0
                if side == "FLAT":
                    w = 0.0
                    side = "FLAT"

                # apply per-symbol cap
                w = _clamp(abs(w), 0.0, _symbol_cap(s))

                if side == "SHORT":
                    w = -float(w)

                # ------------------------------------------------------
                # Execution Regime Sizing (feature-aware sizing)
                # ------------------------------------------------------
                if PORTFOLIO_USE_EXEC_REGIME and abs(float(w)) > 0.0:
                    _sgn = -1.0 if float(w) < 0 else 1.0
                    _mag = abs(float(w))

                    try:
                        skew_z = float(_get_factor_feature_asof(con, "options.skew_25d_z", int(now_ms)))
                        flow_z = float(_get_factor_feature_asof(con, "flows.index_constituent_imbalance_z", int(now_ms)))
                    except Exception:
                        skew_z = 0.0
                        flow_z = 0.0
# =========================
# SECTION 6 / ~200 lines
# =========================

                    stress_mag = max(
                        0.0,
                        max(
                            abs(skew_z) - float(PORTFOLIO_EXEC_SKEW_Z_TH),
                            abs(flow_z) - float(PORTFOLIO_EXEC_FLOW_Z_TH),
                        ),
                    )

                    stress_mult = 1.0
                    if stress_mag > 0.0:
                        stress_mult = float(
                            _clamp(
                                1.0 - (stress_mag * float(PORTFOLIO_EXEC_STRESS_SIZE_REDUCTION)),
                                float(PORTFOLIO_EXEC_REGIME_FLOOR),
                                1.0,
                            )
                        )

                    # earnings proximity (nearest date)
                    earnings_mult = 1.0
                    try:
                        row_e = con.execute(
                            """
                            SELECT earnings_date
                            FROM earnings_calendar
                            WHERE symbol=?
                            ORDER BY ABS(julianday(earnings_date) - julianday(date('now'))) ASC
                            LIMIT 1
                            """,
                            (str(s),),
                        ).fetchone()

                        if row_e and row_e[0]:
                            jd = con.execute(
                                "SELECT (julianday(?) - julianday(date('now')))",
                                (str(row_e[0]),),
                            ).fetchone()
                            if jd and jd[0] is not None:
                                days = float(jd[0])
                                decay = math.exp(-abs(days) / 5.0)
                                earnings_mult = float(
                                    _clamp(
                                        1.0 - (decay * float(PORTFOLIO_EXEC_EARNINGS_SIZE_REDUCTION)),
                                        float(PORTFOLIO_EXEC_REGIME_FLOOR),
                                        1.0,
                                    )
                                )
                    except Exception:
                        earnings_mult = 1.0

                    _mag = float(_mag) * float(stress_mult) * float(earnings_mult)
                    w = float(_sgn) * float(_mag)

                # preserve fields expected downstream
                reason = (tgt or {}).get("reason", {})
                if not isinstance(reason, dict):
                    reason = {"raw": reason}

                try:
                    reason["confidence"] = float(tgt.get("confidence", reason.get("confidence", 0.0)))
                except Exception:
                    reason["confidence"] = 0.0

                exj = (tgt or {}).get("explain_json", "{}")
                if exj is None:
                    exj = "{}"
                exj = str(exj)

                src_id = (tgt or {}).get("source_alert_id", None)
                try:
                    src_id = int(src_id) if src_id is not None else None
                except Exception:
                    src_id = None

                # --- Regime Vector Injection ---
                try:
                    from dev_core.regime_stack import compute_regime_vector, regime_compatibility

                    regime_vector = compute_regime_vector(s)

                    try:
                        regime_profile = getattr(strat, "get_regime_profile", lambda: {})()
                    except Exception:
                        regime_profile = {}

                    compat = regime_compatibility(regime_profile, regime_vector)
                    w = float(w) * float(compat)
                except Exception:
                    regime_vector = {}
                    compat = 1.0

                norm[s] = {
                    "side": side,
                    "weight": float(w),
                    "source_alert_id": src_id,
                    "reason": reason,
                    "explain_json": exj,
                    "regime_vector": regime_vector,
                    "regime_compatibility": compat,
                }

            except Exception:
                continue

        desired = norm

        # renormalize gross
        grossE = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
        if grossE > float(PORTFOLIO_GROSS_CAP) and grossE > 1e-9:
            scaleE = float(PORTFOLIO_GROSS_CAP) / float(grossE)
            for sym in list(desired.keys()):
                desired[sym]["weight"] = float(desired[sym]["weight"]) * float(scaleE)

        # ---            -- ------------------------------------------------------
        # Auto blacklist enforcement (skip symbols temporarily banned)
        # ---            -- ------------------------------------------------------
        try:
            for sym in list(desired.keys()):
                if is_blacklisted(con, sym, now_ms=int(now_ms)):
                    desired.pop(sym, None)
        except Exception:
            pass

        # ---            -- ------------------------------------------------------
        # Exploration cap: if symbol has few realized labels, cap weight
        # ---            -- ------------------------------------------------------
        try:
            for sym in list(desired.keys()):
                row = con.execute(
                    "SELECT COUNT(1) FROM labels WHERE symbol=?",
                    (str(sym),),
                ).fetchone()
                nlab = int(row[0] or 0) if row else 0

                if nlab < int(PORTFOLIO_EXPLORE_MIN_LABELS):
                    # Cap exposure for exploration symbols
                    w0 = float(desired[sym].get("weight", 0.0) or 0.0)
                    sgn = -1.0 if w0 < 0 else 1.0
                    wabs = float(_clamp(abs(w0), 0.0, float(PORTFOLIO_EXPLORE_MAX_W)))
                    desired[sym]["weight"] = float(sgn * wabs)
                    # Tag for explainability/debugging
                    try:
                        r = desired[sym].get("reason") or {}
                        if isinstance(r, dict):
                            r["explore_cap"] = True
                            r["labels_n"] = int(nlab)
                            desired[sym]["reason"] = r
                    except Exception:
                        pass
        except Exception:
            pass

        # ---            -- ------------------------------------------------------
        # Capital allocation optimizer (return vs drawdown utility)
        # ---            -- ------------------------------------------------------
        try:
            desired = _optimize_capital_allocation(con, desired)
        except Exception:
            pass

        # ---            -- ------------------------------------------------------
        # Impact-aware sizing (penalize symbols with bad realized slippage)
        # ---            -- ------------------------------------------------------
        try:
            desired = _apply_impact_aware_sizing(con, desired)
        except Exception:
            pass

        # hard cap max positions (keep largest abs weights) (dynamic under preserve)
        eff_max_pos = int(_eff_max_positions())
        if eff_max_pos >= 0 and len(desired) > int(eff_max_pos):
            items = sorted(
                desired.items(),
                key=lambda kv: abs(float((kv[1] or {}).get("weight", 0.0))),
                reverse=True,
            )
            desired = dict(items[: int(eff_max_pos)])

        # ---            -- ------------------------------------------------------
        # Correlation-aware convex optimizer (preferred) OR prune fallback
        # ---            -- ------------------------------------------------------
        if PORTFOLIO_CORR_OPT and len(desired) > 1:
            try:
                # --- adaptive gamma by regime ---
                gamma_eff = float(PORTFOLIO_CORR_OPT_GAMMA_BASE)
                regime_name = "MID"

                try:
                    from dev_core.regime_size import regime_capital_scale

                    _rs = regime_capital_scale(con=con, anchor=str(PORTFOLIO_REGIME_ANCHOR))
                    regime_name = str((_rs or {}).get("regime") or "MID").upper()
                except Exception:
                    regime_name = "MID"

                if regime_name == "LOW":
                    gamma_eff *= float(PORTFOLIO_CORR_OPT_GAMMA_LOW)
                elif regime_name == "HIGH":
                    gamma_eff *= float(PORTFOLIO_CORR_OPT_GAMMA_HIGH)
                else:
                    gamma_eff *= float(PORTFOLIO_CORR_OPT_GAMMA_MID)

                from dev_core.corr_opt import corr_aware_optimize_desired

                desired = corr_aware_optimize_desired(
                    con,
                    desired,
                    gross_cap=float(PORTFOLIO_GROSS_CAP),
                    lookback=int(PORTFOLIO_CORR_LOOKBACK),
                    corr_max=float(PORTFOLIO_CORR_MAX),
                    gamma=float(gamma_eff),
                    ridge=float(PORTFOLIO_CORR_OPT_RIDGE),
                    iters=int(PORTFOLIO_CORR_OPT_ITERS),
                )

                # annotate for auditability
                for sym in list(desired.keys()):
                    desired[sym].setdefault("reason", {})
                    desired[sym]["reason"]["corr_opt_gamma_eff"] = float(gamma_eff)
                    desired[sym]["reason"]["corr_opt_regime"] = str(regime_name)

            except Exception:
                pass

        elif PORTFOLIO_CORR_PRUNE and len(desired) > 1:
            try:
                from dev_core.risk import corr_from_prices

                kept = []
                items = sorted(
                    desired.items(),
                    key=lambda kv: abs(float(kv[1].get("weight", 0.0))),
                    reverse=True,
                )

                for sym, tgt in items:
                    ok = True
                    for ks in kept:
                        c = corr_from_prices(con, sym, ks, lookback=int(PORTFOLIO_CORR_LOOKBACK))
                        if c is not None and abs(float(c)) >= float(PORTFOLIO_CORR_MAX):
                            ok = False
                            break
                    if ok:
                        kept.append(sym)

                desired = {s: desired[s] for s in kept if s in desired}
            except Exception:
                pass

        # safety: enforce portfolio gross cap even if strategy already normalized
        gross = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
        if gross > float(PORTFOLIO_GROSS_CAP) and gross > 1e-9:
            scale = float(PORTFOLIO_GROSS_CAP) / float(gross)
            for sym in list(desired.keys()):
                w0 = float(desired[sym].get("weight", 0.0) or 0.0)
                sign = -1.0 if w0 < 0 else 1.0
                desired[sym]["weight"] = sign * abs(float(w0) * float(scale))

        # ---            -- ------------------------------------------------------
        # A) VOL TARGETING (opt-in): scale weights by realized vol
        # ---            -- ------------------------------------------------------
        try:
            from dev_core.risk import PORTFOLIO_USE_VOL_TARGET, realized_vol_from_prices, vol_scale_weight

            if PORTFOLIO_USE_VOL_TARGET:
                for sym in list(desired.keys()):
                    vol = realized_vol_from_prices(con, sym)
                    if vol is None:
                        continue
                    desired[sym]["weight"] = float(vol_scale_weight(desired[sym]["weight"], vol))
                # renormalize gross after scaling
                gross2 = sum(abs(float(v["weight"])) for v in desired.values())
                if gross2 > float(PORTFOLIO_GROSS_CAP) and gross2 > 1e-9:
                    scale2 = float(PORTFOLIO_GROSS_CAP) / float(gross2)
                    for sym in list(desired.keys()):
                        w0 = float(desired[sym].get("weight", 0.0) or 0.0)
                        sign = -1.0 if w0 < 0 else 1.0
                        desired[sym]["weight"] = sign * abs(float(w0) * float(scale2))
        except Exception:
            pass
# =========================
# SECTION 7 / ~200 lines
# =========================

        # ---            -- ------------------------------------------------------
        # B) STRESS / REGIME GATE (opt-in): compress exposure under stress
        # Now incorporates:
        #   • VIX stress
        #   • Options skew z-score
        #   • Index constituent flow imbalance z-score
        # ---            -- ------------------------------------------------------
        try:
            if PORTFOLIO_USE_STRESS_GATE and desired:
                # --- VIX baseline ---
                st = _vix_stress(con)
                vix_z = float(st.get("z", 0.0))
                f_vix = float(_stress_factor_from_vix_z(vix_z))

                # --- Skew + Flow regime factors (latest as-of now_ms) ---
                skew_z = 0.0
                flow_z = 0.0

                try:
                    row = con.execute(
                        """
                        SELECT value
                        FROM factor_features
                        WHERE feature_id='options.skew_25d_z'
                        ORDER BY asof_ts DESC
                        LIMIT 1
                        """
                    ).fetchone()
                    if row:
                        skew_z = float(row[0] or 0.0)
                except Exception:
                    skew_z = 0.0

                try:
                    row = con.execute(
                        """
                        SELECT value
                        FROM factor_features
                        WHERE feature_id='flows.index_constituent_imbalance_z'
                        ORDER BY asof_ts DESC
                        LIMIT 1
                        """
                    ).fetchone()
                    if row:
                        flow_z = float(row[0] or 0.0)
                except Exception:
                    flow_z = 0.0

                # --- Stress magnitude beyond thresholds ---
                skew_excess = max(0.0, abs(skew_z) - 1.5)
                flow_excess = max(0.0, abs(flow_z) - 2.0)

                stress_mag = max(skew_excess, flow_excess)

                # compression factor (bounded)
                f_struct = 1.0
                if stress_mag > 0.0:
                    f_struct = max(0.20, 1.0 - (0.35 * stress_mag))

                # combined factor
                f_total = float(min(f_vix, f_struct))

                if f_total < 1.0:
                    for sym in list(desired.keys()):
                        desired[sym]["weight"] = float(desired[sym]["weight"]) * float(f_total)
                        desired[sym].setdefault("reason", {})
                        desired[sym]["reason"]["stress_gate_factor"] = float(f_total)
                        desired[sym]["reason"]["stress_vix_z"] = float(vix_z)
                        desired[sym]["reason"]["stress_skew_z"] = float(skew_z)
                        desired[sym]["reason"]["stress_flow_z"] = float(flow_z)

                # renormalize gross after compression
                gross_s = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
                eff_cap = float(_eff_gross_cap())
                if gross_s > float(eff_cap) and gross_s > 1e-9:
                    scale_s = float(eff_cap) / float(gross_s)
                    for sym in list(desired.keys()):
                        w0 = float(desired[sym].get("weight", 0.0) or 0.0)
                        sign = -1.0 if w0 < 0 else 1.0
                        desired[sym]["weight"] = sign * abs(float(w0) * float(scale_s))

        except Exception:
            pass

        # ---            -- ------------------------------------------------------
        # B2) SOCIAL GATE (opt-in): block/downsizing under manipulation risk
        # ---            -- ------------------------------------------------------
        try:
            if PORTFOLIO_USE_SOCIAL_GATE and desired:
                from dev_core.social_risk import social_gate_for_symbol

                for sym in list(desired.keys()):
                    g = social_gate_for_symbol(
                        con,
                        str(sym),
                        int(now_ms),
                        bucket_sec=int(PORTFOLIO_SOCIAL_BUCKET_SEC),
                        manip_block_th=float(PORTFOLIO_SOCIAL_MANIP_BLOCK_TH),
                        shock_th=float(PORTFOLIO_SOCIAL_ATTEN_SHOCK_TH),
                        shock_factor=float(PORTFOLIO_SOCIAL_SHOCK_FACTOR),
                    ) or {}

                    if g.get("block"):
                        desired[sym]["weight"] = 0.0
                        desired[sym]["side"] = "FLAT"
                        desired[sym].setdefault("reason", {})
                        desired[sym]["reason"]["social_gate_block"] = 1
                        desired[sym]["reason"]["social_manip_risk"] = float(g.get("manip_risk", 0.0))
                        desired[sym]["reason"]["social_attention_shock"] = float(g.get("attention_shock", 0.0))
                        desired[sym]["reason"]["social_promo_likelihood"] = float(g.get("promo_likelihood_mean", 0.0))
                        continue

                    f = float(g.get("factor", 1.0))
                    if f < 1.0:
                        desired[sym]["weight"] = float(desired[sym]["weight"]) * f
                        desired[sym].setdefault("reason", {})
                        desired[sym]["reason"]["social_gate_factor"] = float(f)
                        desired[sym]["reason"]["social_manip_risk"] = float(g.get("manip_risk", 0.0))
                        desired[sym]["reason"]["social_attention_shock"] = float(g.get("attention_shock", 0.0))
                        desired[sym]["reason"]["social_promo_likelihood"] = float(g.get("promo_likelihood_mean", 0.0))

                # renormalize gross after social compression (still respect gross cap)
                gross_soc = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
                if gross_soc > float(PORTFOLIO_GROSS_CAP) and gross_soc > 1e-9:
                    scale_soc = float(PORTFOLIO_GROSS_CAP) / float(gross_soc)
                    for sym in list(desired.keys()):
                        w0 = float(desired[sym].get("weight", 0.0) or 0.0)
                        sign = -1.0 if w0 < 0 else 1.0
                        desired[sym]["weight"] = sign * abs(float(w0) * float(scale_soc))
        except Exception:
            pass

        # ---            -- ------------------------------------------------------
        # C) VOL-OF-VOL GATE (opt-in): per-symbol compression in unstable regimes
        # Uses price-only proxy from dev_core.tech_indicators (if present).
        # ---            -- ------------------------------------------------------
        try:
            if PORTFOLIO_USE_VOV_GATE and desired:
                try:
                    from dev_core.tech_indicators import compute_tech_features
                except Exception:
                    compute_tech_features = None

                if compute_tech_features:
                    now_ms2 = _now_ms()
                    a = float(PORTFOLIO_VOV_ALPHA)
                    v_lo = float(PORTFOLIO_VOV_FLOOR)
                    v_hi = float(PORTFOLIO_VOV_CEIL)
                    span = max(1e-12, (v_hi - v_lo))

                    for sym in list(desired.keys()):
                        tf = compute_tech_features(str(sym), int(now_ms2)) or {}
                        vv = float(tf.get("vol_of_vol", 0.0))

                        # normalize vv into [0,1] then apply penalty
                        x = (vv - v_lo) / span
                        x = float(_clamp(x, 0.0, 1.0))

                        # factor = 1/(1 + alpha*x)
                        f = float(1.0 / (1.0 + a * x))
                        desired[sym]["weight"] = float(desired[sym]["weight"]) * float(f)

                        desired[sym].setdefault("reason", {})
                        desired[sym]["reason"]["vov_gate_factor"] = float(f)
                        desired[sym]["reason"]["vov_value"] = float(vv)

                    # renormalize gross after vov compression
                    gross_v = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
                    if gross_v > float(PORTFOLIO_GROSS_CAP) and gross_v > 1e-9:
                        scale_v = float(PORTFOLIO_GROSS_CAP) / float(gross_v)
                        for sym in list(desired.keys()):
                            desired[sym]["weight"] = float(desired[sym]["weight"]) * float(scale_v)
        except Exception:
            pass
# =========================
# SECTION 8 / ~200 lines
# =========================

        # ---            -- ------------------------------------------------------
        # Phase 5.2: POSITION SIZE POLICY (confidence -> factor)
        # (must happen BEFORE orders are emitted)
        # ---            -- ------------------------------------------------------
        try:
            from dev_core.size_policy import load_latest_size_policy, size_factor
            from dev_core.drawdown_state import get_current_drawdown

            pol = load_latest_size_policy(con)
            if pol:
                dd = float(get_current_drawdown(con))
                for sym in list(desired.keys()):
                    try:
                        conf = float((desired[sym].get("reason") or {}).get("confidence", 0.0))
                    except Exception:
                        conf = 0.0
                    f = float(size_factor(pol, conf, drawdown=dd))

                    desired[sym]["weight"] = float(desired[sym]["weight"]) * f

                    # annotate for auditability
                    desired[sym].setdefault("reason", {})
                    desired[sym]["reason"]["size_factor"] = float(f)
                    desired[sym]["reason"]["size_policy_ts_ms"] = int(pol.get("ts_ms", 0))
                    desired[sym]["reason"]["drawdown_for_sizing"] = float(dd)
        except Exception:
            pass

        # ---            -- ------------------------------------------------------
        # Phase 5.3: EXECUTION REALISM (opt-in)
        # - blocks/downsizes intents when symbol prices are stale
        # - downsizes under elevated stress (VIX z) if VIX is present
        # - downsizes under high ATR% (volatility proxy)
        # ---            -- ------------------------------------------------------
        if PORTFOLIO_USE_EXEC_REALISM:
            for sym in list(desired.keys()):
                try:
                    ef, meta = _execution_realism_factor(con, sym, int(now_ms))
                except Exception:
                    ef, meta = 1.0, {
                        "staleness_sec": 0.0,
                        "stress_vix_z_60": 0.0,
                        "atr_pct": 0.0,
                        "slippage_bps_est": 0.0,
                    }

                desired[sym]["weight"] = float(desired[sym].get("weight", 0.0)) * float(ef)

                # annotate for auditability/explainability
                desired[sym].setdefault("reason", {})
                desired[sym]["reason"]["exec_realism_factor"] = float(ef)
                desired[sym]["reason"]["exec_staleness_sec"] = float(meta.get("staleness_sec", 0.0))
                desired[sym]["reason"]["exec_stress_vix_z_60"] = float(meta.get("stress_vix_z_60", 0.0))
                desired[sym]["reason"]["exec_atr_pct"] = float(meta.get("atr_pct", 0.0))
                desired[sym]["reason"]["exec_slippage_bps_est"] = float(meta.get("slippage_bps_est", 0.0))

        # renormalize gross after size policy scaling
        gross3 = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
        if gross3 > float(PORTFOLIO_GROSS_CAP) and gross3 > 1e-9:
            scale3 = float(PORTFOLIO_GROSS_CAP) / float(gross3)
            for sym in list(desired.keys()):
                desired[sym]["weight"] = float(desired[sym]["weight"]) * float(scale3)
        # ---            -- ------------------------------------------------------
        # B2) CAPITAL PRESERVATION MODE: compress gross exposure + annotate reasons
        # ---            -- ------------------------------------------------------
        try:
            if _capital_mode() == "preserve" and desired:
                cap = float(_eff_gross_cap())
                gross0 = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
                if gross0 > cap and gross0 > 1e-9:
                    scale_cap = float(cap) / float(gross0)
                    for sym in list(desired.keys()):
                        desired[sym]["weight"] = float(desired[sym]["weight"]) * float(scale_cap)
                        desired[sym].setdefault("reason", {})
                        desired[sym]["reason"]["capital_mode"] = "preserve"
                        desired[sym]["reason"]["capital_preserve_gross_cap"] = float(cap)
                        desired[sym]["reason"]["capital_preserve_scale"] = float(scale_cap)
        except Exception:
            pass

        # ---            -- ------------------------------------------------------
        # Phase 6: REGIME-ADAPTIVE CAPITAL SCALING (base * confidence * VIX * drawdown)
        # ---            -- ------------------------------------------------------
        try:
            from dev_core.regime_size import regime_capital_scale
            from dev_core.opportunity_allocation import opportunity_weight  # imported in original; preserved

            _rs = regime_capital_scale(con=con, anchor=str(PORTFOLIO_REGIME_ANCHOR))
            mult = float((_rs or {}).get("final_mult", 1.0))

            # persist last scaling decision (auditability)
            try:
                _put_meta(
                    con,
                    "last_regime_scaling",
                    json.dumps(_rs or {}, separators=(",", ":"), sort_keys=True),
                )
            except Exception:
                pass

            if mult != 1.0:
                for sym in list(desired.keys()):
                    try:
                        desired[sym]["weight"] = float(desired[sym].get("weight", 0.0) or 0.0) * float(mult)
                        desired[sym].setdefault("reason", {})
                        desired[sym]["reason"]["regime_anchor"] = str(
                            (_rs or {}).get("anchor") or str(PORTFOLIO_REGIME_ANCHOR)
                        )
                        desired[sym]["reason"]["regime"] = str((_rs or {}).get("regime") or "")
                        desired[sym]["reason"]["regime_base_mult"] = float((_rs or {}).get("base_mult", 1.0))
                        desired[sym]["reason"]["regime_conf"] = float((_rs or {}).get("conf", 1.0))
                        desired[sym]["reason"]["regime_conf_mult"] = float((_rs or {}).get("conf_mult", 1.0))
                        desired[sym]["reason"]["regime_vix_z"] = (_rs or {}).get("vix_z", None)
                        desired[sym]["reason"]["regime_vix_mult"] = float((_rs or {}).get("vix_mult", 1.0))
                        desired[sym]["reason"]["regime_dd"] = (_rs or {}).get("dd", None)
                        desired[sym]["reason"]["regime_dd_mult"] = float((_rs or {}).get("dd_mult", 1.0))
                        desired[sym]["reason"]["regime_final_mult"] = float((_rs or {}).get("final_mult", 1.0))
                    except Exception:
                        pass

                # renormalize gross after regime scaling
                grossR = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
                if grossR > float(PORTFOLIO_GROSS_CAP) and grossR > 1e-9:
                    scaleR = float(PORTFOLIO_GROSS_CAP) / float(grossR)
                    for sym in list(desired.keys()):
                        desired[sym]["weight"] = float(desired[sym]["weight"]) * float(scaleR)
        except Exception:
            pass

        # ---            -- ------------------------------------------------------
        # Phase 2: PORTFOLIO HARD RISK GATE (net / turnover / dd add-block)
        # ---            -- ------------------------------------------------------
        try:
            desired, _gate = apply_portfolio_risk_gate(con, desired, state, now_ms=int(now_ms))
            try:
                _put_meta(
                    con,
                    "last_risk_gate",
                    json.dumps(_gate or {}, separators=(",", ":"), sort_keys=True),
                )
            except Exception:
                pass
        except Exception:
            _gate = None

        # ---            -- ------------------------------------------------------
        # Burst control: temporal clustering dampener
        # ---            -- ------------------------------------------------------
        try:
            desired = _apply_temporal_dampener(con, desired, now_ms=int(now_ms))
        except Exception:
            pass

        # ---            -- ------------------------------------------------------
        # Capital-at-Risk gate (tail-risk budget)
        # ---            -- ------------------------------------------------------
        try:
            desired, _car = _apply_capital_at_risk_gate(desired)
            try:
                _put_meta(
                    con,
                    "last_capital_at_risk",
                    json.dumps(_car or {}, separators=(",", ":"), sort_keys=True),
                )
            except Exception:
                pass
        except Exception:
            pass

        # perform rebalance under one transaction
        con.execute("BEGIN IMMEDIATE;")

        orders_n = 0
        changed = []
# =========================
# SECTION 9 / ~200 lines
# =========================

        # 1) handle symbols in desired set
        for sym, tgt in desired.items():
            cur = state.get(sym)
            to_side = tgt["side"]
            to_w = float(tgt["weight"])
            source_alert_id = tgt["source_alert_id"]

            explain = {
                "strategy": {
                    "name": "multi_strategy",
                    "rl_score": None,
                    "min_conf": PORTFOLIO_MIN_CONF,
                    "min_abs_z": PORTFOLIO_MIN_ABS_Z,
                    "max_positions": PORTFOLIO_MAX_POSITIONS,
                    "gross_cap": PORTFOLIO_GROSS_CAP,
                    "max_w_per_symbol": PORTFOLIO_MAX_W_PER_SYMBOL,
                    "score_norm": PORTFOLIO_SCORE_NORM,
                    "min_hold_s": PORTFOLIO_MIN_HOLD_S,
                    "lookback_s": PORTFOLIO_LOOKBACK_S,
                },
                "selector": {
                    "rule_choice": str(_get_meta(con, "last_rule_choice") or "multi_strategy"),
                    "rl_choice": str(_get_meta(con, "last_rl_choice") or "multi_strategy"),
                    "rl_score": float(_get_meta(con, "last_rl_score") or 0.0),
                },
                "signal": tgt["reason"],
                "tradability": _tradability_from_explain(tgt.get("explain_json", "{}")),
            }

            if not cur:
                # open new
                _write_state_row(con, sym, to_side, to_w, now_ms, now_ms, source_alert_id, tgt["explain_json"])
                _emit_order(con, sym, "OPEN", "FLAT", to_side, 0.0, to_w, source_alert_id, explain)
                orders_n += 1
                changed.append(sym)
                continue

            from_side = str(cur["side"])
            from_w = float(cur["weight"])
            opened_ts = int(cur["opened_ts_ms"])
            age_s = max(0.0, (now_ms - opened_ts) / 1000.0)

            # reversal hold guard
            if from_side in ("LONG", "SHORT") and to_side != from_side and age_s < float(PORTFOLIO_MIN_HOLD_S):
                # HOLD (no change)
                explain["hold_reason"] = f"min_hold not met (age_s={age_s:.1f} < {PORTFOLIO_MIN_HOLD_S})"
                _emit_order(con, sym, "HOLD", from_side, from_side, from_w, from_w, source_alert_id, explain)
                orders_n += 1
                continue

            # compute action
            if from_side == "FLAT" and to_side != "FLAT" and to_w > 0:
                _write_state_row(con, sym, to_side, to_w, now_ms, now_ms, source_alert_id, tgt["explain_json"])
                _emit_order(con, sym, "OPEN", "FLAT", to_side, 0.0, to_w, source_alert_id, explain)
                orders_n += 1
                changed.append(sym)
                continue

            if from_side in ("LONG", "SHORT") and to_side == from_side:
                if abs(to_w - from_w) < 1e-6:
                    _emit_order(con, sym, "HOLD", from_side, from_side, from_w, from_w, source_alert_id, explain)
                    orders_n += 1
                elif to_w > from_w:
                    _write_state_row(con, sym, from_side, to_w, opened_ts, now_ms, source_alert_id, tgt["explain_json"])
                    _emit_order(con, sym, "INCREASE", from_side, from_side, from_w, to_w, source_alert_id, explain)
                    orders_n += 1
                    changed.append(sym)
                else:
                    _write_state_row(con, sym, from_side, to_w, opened_ts, now_ms, source_alert_id, tgt["explain_json"])
                    _emit_order(con, sym, "DECREASE", from_side, from_side, from_w, to_w, source_alert_id, explain)
                    orders_n += 1
                    changed.append(sym)
                continue

            if from_side in ("LONG", "SHORT") and to_side != from_side:
                # reverse
                _write_state_row(con, sym, to_side, to_w, now_ms, now_ms, source_alert_id, tgt["explain_json"])
                _emit_order(con, sym, "REVERSE", from_side, to_side, from_w, to_w, source_alert_id, explain)
                orders_n += 1
                changed.append(sym)
                continue

            # fallback: hold
            _emit_order(con, sym, "HOLD", from_side, from_side, from_w, from_w, source_alert_id, explain)
            orders_n += 1

        # 2) close symbols not desired anymore (but only if currently open)
        for sym, cur in state.items():
            if sym in desired:
                continue
            from_side = str(cur["side"])
            from_w = float(cur["weight"])
            opened_ts = int(cur["opened_ts_ms"])
            age_s = max(0.0, (now_ms - opened_ts) / 1000.0)

            if from_side in ("LONG", "SHORT") and from_w > 0:
                # optional: min hold before closing too
                if age_s < float(PORTFOLIO_MIN_HOLD_S):
                    explain = {
                        "hold_reason": f"min_hold not met for close (age_s={age_s:.1f} < {PORTFOLIO_MIN_HOLD_S})"
                    }
                    _emit_order(con, sym, "HOLD", from_side, from_side, from_w, from_w, None, explain)
                    orders_n += 1
                    continue

                _write_state_row(con, sym, "FLAT", 0.0, now_ms, now_ms, None, "{}")
                _emit_order(con, sym, "CLOSE", from_side, "FLAT", from_w, 0.0, None, {"reason": "no longer selected"})
                orders_n += 1
                changed.append(sym)

        # (size policy applied earlier, before order emission)
        # ---            -- ------------------------------------------------------
        # Update live drawdown meta (equity proxy from weights)
        # ---            -- ------------------------------------------------------
        try:
            # Simple proxy: peak gross vs current gross
            gross_now = sum(abs(float(v.get("weight", 0.0))) for v in desired.values())
            peak_raw = _get_meta(con, "peak_gross_weight")
            peak = float(peak_raw) if peak_raw is not None else gross_now
            if gross_now > peak:
                peak = gross_now
            drawdown = (peak - gross_now) if peak > 1e-9 else 0.0

            _set_meta(con, "peak_gross_weight", str(float(peak)))
            _set_meta(con, "last_drawdown", str(float(drawdown)))
        except Exception:
            pass

        # update live drawdown meta (from broker if available)
        try:
            from dev_core.broker_sim import broker_snapshot

            snap = broker_snapshot(limit_fills=0)
            if snap and snap.get("ok"):
                dd = snap.get("account", {}).get("drawdown")
                if dd is not None:
                    _set_meta(con, "live_drawdown", str(float(dd)))
        except Exception:
            pass

        _set_meta(con, "last_strategy_name", "multi_strategy")
        _set_meta(con, "last_rebalance_ts_ms", str(now_ms))
        con.commit()

        return {
            "ok": True,
            "strategy": "multi_strategy",
            "changed": changed,
            "orders_n": int(orders_n),
            "selected": list(desired.keys()),
        }

    except Exception as e:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        return {"ok": False, "error": str(e)}
    finally:
        con.close()
# =========================
# SECTION 10 / remainder
# =========================

def get_portfolio_snapshot(limit_orders: int = 50) -> Dict:
    init_portfolio_db()
    con = connect()
    try:
        state = con.execute(
            """
            SELECT symbol, side, weight, opened_ts_ms, updated_ts_ms, source_alert_id, explain_json
            FROM portfolio_state
            ORDER BY symbol
            """
        ).fetchall()

        orders = con.execute(
            """
            SELECT ts_ms, symbol, action, from_side, to_side, from_weight, to_weight, delta_weight, source_alert_id, explain_json
            FROM portfolio_orders
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (int(max(1, min(500, int(limit_orders)))),),
        ).fetchall()

        out_state = []
        for r in state or []:
            out_state.append(
                {
                    "symbol": r[0],
                    "side": r[1],
                    "weight": float(r[2]),
                    "opened_ts_ms": int(r[3]),
                    "updated_ts_ms": int(r[4]),
                    "source_alert_id": (int(r[5]) if r[5] is not None else None),
                    "explain_json": str(r[6] or "{}"),
                }
            )

        out_orders = []
        for r in orders or []:
            out_orders.append(
                {
                    "ts_ms": int(r[0]),
                    "symbol": str(r[1]),
                    "action": str(r[2]),
                    "from_side": str(r[3]),
                    "to_side": str(r[4]),
                    "from_weight": float(r[5]),
                    "to_weight": float(r[6]),
                    "delta_weight": float(r[7]),
                    "source_alert_id": (int(r[8]) if r[8] is not None else None),
                    "explain_json": str(r[9] or "{}"),
                }
            )

        return {"ok": True, "state": out_state, "orders": out_orders}
    finally:
        con.close()
