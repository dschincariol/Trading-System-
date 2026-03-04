# dev_core/broker_sim.py
"""
Broker simulator (paper execution).

Consumes portfolio_orders (intents) and writes:
- broker_account (cash/equity baseline)
- broker_positions (qty, avg_px)
- broker_fills (fills history)
- broker_meta (cursor for last applied portfolio_orders id)

Assumptions:
- Uses latest price <= fill_ts for each symbol (from prices table).
- Converts target weights into target qty: qty = target_weight * equity / px
- SHORT => negative qty

Broker realism knobs:
- Spread + slippage in execution price
- Fees (bps of notional)
- Chunking + per-chunk latency
- Max trade notional cap per apply pass (% of equity)
"""

import json
import os
import time
import math
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from engine.storage import connect
from engine.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot

# -----------------------------
# Small numeric guards
# -----------------------------
def _is_finite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except Exception:
        return False


def _safe_f(x, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else float(default)
    except Exception:
        return float(default)


def _safe_i(x, default: int = 0) -> int:
    try:
        v = int(x)
        return v
    except Exception:
        return int(default)


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except Exception:
        v = float(lo)
    return float(max(float(lo), min(float(hi), v)))


def _u01(seed: str) -> float:
    """
    Deterministic pseudo-random in [0,1) from a stable seed string.
    (No global RNG; reproducible in audits.)
    """
    try:
        h = hashlib.sha256(str(seed).encode("utf-8")).hexdigest()
        # 12 hex chars ~ 48 bits
        n = int(h[:12], 16)
        return (n % 10_000_000) / 10_000_000.0
    except Exception:
        return 0.0


# -----------------------------
# Execution ROI conditioning (no sentiment, no LLM)
# -----------------------------

_EARNINGS_HALF_LIFE_DAYS = float(os.environ.get("EARNINGS_HALF_LIFE_DAYS", "5.0"))
_EXEC_SKEW_Z_THRESH = float(os.environ.get("EXEC_SKEW_Z_THRESH", "1.5"))
_EXEC_FLOW_Z_THRESH = float(os.environ.get("EXEC_FLOW_Z_THRESH", "2.0"))
_EXEC_EARNINGS_SIZE_MAX_REDUCTION = float(os.environ.get("EXEC_EARNINGS_SIZE_MAX_REDUCTION", "0.55"))
_EXEC_STRESS_SIZE_MAX_REDUCTION = float(os.environ.get("EXEC_STRESS_SIZE_MAX_REDUCTION", "0.35"))
_EXEC_EARNINGS_SLIP_ADD_BPS = float(os.environ.get("EXEC_EARNINGS_SLIP_ADD_BPS", "0.75"))
_EXEC_STRESS_SLIP_ADD_BPS = float(os.environ.get("EXEC_STRESS_SLIP_ADD_BPS", "0.75"))
_EXEC_STRESS_LATENCY_MULT_MAX = float(os.environ.get("EXEC_STRESS_LATENCY_MULT_MAX", "2.0"))


def _clamp01(x: float) -> float:
    return _clamp(float(x), 0.0, 1.0)


def _get_factor_feature_asof(con, feature_id: str, ts_ms: int) -> float:
    try:
        q_ts = int(ts_ms) - 1
        row = con.execute(
            """
            SELECT value
            FROM factor_features
            WHERE feature_id=?
              AND asof_ts < ?
              AND effective_ts < ?
            ORDER BY asof_ts DESC, effective_ts DESC
            LIMIT 1
            """,
            (str(feature_id), q_ts, q_ts),
        ).fetchone()
        if not row:
            return 0.0
        return _safe_f(row[0], 0.0)
    except Exception:
        return 0.0


def _ymd_from_ts_ms(ts_ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts_ms) / 1000.0))


def _earnings_proximity_decay(con, symbol: str, ts_ms: int) -> float:
    """
    Returns [0,1]. 1.0 = very near earnings date, 0.0 = far.
    Uses nearest earnings_calendar row by date distance.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return 0.0

    try:
        today = _ymd_from_ts_ms(int(ts_ms))
        row = con.execute(
            """
            SELECT earnings_date
            FROM earnings_calendar
            WHERE symbol=?
            ORDER BY ABS(julianday(earnings_date) - julianday(?)) ASC
            LIMIT 1
            """,
            (sym, str(today)),
        ).fetchone()
        if not row:
            return 0.0

        ed = str(row[0] or "").strip()
        if not ed:
            return 0.0

        jd = con.execute(
            "SELECT (julianday(?) - julianday(?))",
            (str(ed), str(today)),
        ).fetchone()
        if not jd or jd[0] is None:
            return 0.0

        days = float(jd[0])
        hl = max(0.5, float(_EARNINGS_HALF_LIFE_DAYS))
        return _clamp01(math.exp(-abs(days) / hl))
    except Exception:
        return 0.0


# -----------------------------
# Broker realism knobs (env)
# -----------------------------
BROKER_SPREAD_BPS = float(os.environ.get("BROKER_SPREAD_BPS", "2.0"))  # total spread (bps)
BROKER_SLIPPAGE_BPS = float(os.environ.get("BROKER_SLIPPAGE_BPS", "1.0"))  # extra slippage (bps)
BROKER_FEE_BPS = float(os.environ.get("BROKER_FEE_BPS", "0.5"))  # commission/fees (bps of notional)
BROKER_MAX_TRADE_PCT_EQUITY = float(os.environ.get("BROKER_MAX_TRADE_PCT_EQUITY", "0.35"))  # cap per apply pass
BROKER_CHUNK_PCT = float(os.environ.get("BROKER_CHUNK_PCT", "0.33"))  # split into chunks
BROKER_LATENCY_MS = int(os.environ.get("BROKER_LATENCY_MS", "120"))  # per chunk latency

# Starting capital / cash baseline (additive; preserves existing behavior if not set)
BROKER_START_CASH = float(os.environ.get("BROKER_START_CASH", "0.0"))
BROKER_START_EQUITY = float(os.environ.get("BROKER_START_EQUITY", "0.0"))  # optional override; usually = cash

# If 0: do not allow cash to go negative (no margin). If 1: allow margin/short proceeds to fund buys.
BROKER_ALLOW_MARGIN = os.environ.get("BROKER_ALLOW_MARGIN", "1") == "1"

# Size-based slippage (impact proxy). 0 disables (keeps constant slippage).
# Applied as: slip_bps = base_slip_bps * (1 + impact_alpha * (notional / equity))
BROKER_IMPACT_ALPHA = float(os.environ.get("BROKER_IMPACT_ALPHA", "1.5"))

# Optional wall-clock latency simulation (default off to preserve throughput)
BROKER_LATENCY_SLEEP = os.environ.get("BROKER_LATENCY_SLEEP", "0") == "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS broker_account (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cash REAL NOT NULL,
  equity REAL NOT NULL,
  updated_ts_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_positions (
  symbol TEXT PRIMARY KEY,
  qty REAL NOT NULL,
  avg_px REAL NOT NULL,
  updated_ts_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  qty REAL NOT NULL,
  px REAL NOT NULL,
  source_order_id INTEGER,
  note TEXT,
  explain_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_broker_fills_ts ON broker_fills(ts_ms);

CREATE TABLE IF NOT EXISTS broker_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS broker_order_state (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_order_id INTEGER,
  symbol TEXT NOT NULL,
  state TEXT NOT NULL,
  created_ts_ms INTEGER NOT NULL,
  updated_ts_ms INTEGER NOT NULL,
  ttl_ms INTEGER,
  meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_broker_order_state_symbol ON broker_order_state(symbol);

"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_tables(con):
    con.executescript(SCHEMA)
    row = con.execute("SELECT cash, equity FROM broker_account WHERE id=1").fetchone()
    if not row:
        ts = _now_ms()
        # Preserve legacy defaults unless user explicitly sets env
        cash0 = float(BROKER_START_CASH or 0.0)

        # If BROKER_START_EQUITY not set, default equity to cash0 (mark-to-market will update later)
        eq0 = float(BROKER_START_EQUITY) if float(BROKER_START_EQUITY or 0.0) > 0.0 else float(cash0)

        # Legacy behavior was (cash=0, equity=1). Keep that only when both are unset/zero.
        if cash0 == 0.0 and eq0 == 0.0:
            cash0 = 0.0
            eq0 = 1.0

        con.execute(
            "INSERT INTO broker_account(id, cash, equity, updated_ts_ms) VALUES(1, ?, ?, ?)",
            (float(cash0), float(eq0), int(ts)),
        )

    con.commit()


def init_broker_db():
    con = connect()
    try:
        _ensure_tables(con)
    finally:
        con.close()


def _read_account(con) -> dict:
    r = con.execute("SELECT cash, equity, updated_ts_ms FROM broker_account WHERE id=1").fetchone()
    if not r:
        return {"cash": 0.0, "equity": 1.0, "updated_ts_ms": 0}
    return {"cash": float(r[0]), "equity": float(r[1]), "updated_ts_ms": int(r[2])}


def _write_account(con, cash: float, equity: float, ts_ms: int):
    con.execute(
        "UPDATE broker_account SET cash=?, equity=?, updated_ts_ms=? WHERE id=1",
        (float(cash), float(equity), int(ts_ms)),
    )


def _get_meta(con, key: str):
    r = con.execute("SELECT value FROM broker_meta WHERE key=?", (str(key),)).fetchone()
    return str(r[0]) if r and r[0] is not None else None


def _set_meta(con, key: str, value: str):
    con.execute(
        """
        INSERT INTO broker_meta(key,value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(key), str(value)),
    )


def _get_price_at_or_before(con, symbol: str, ts_ms: int):
    # fetch the last price strictly before the requested time
    r = con.execute(
        """
        SELECT price, ts_ms
        FROM prices
        WHERE symbol = ? AND ts_ms < ?
        ORDER BY ts_ms DESC
        LIMIT 1
        """,
        (str(symbol), int(ts_ms)),
    ).fetchone()
    if not r:
        # legacy fallback (older schema) - keep behavior
        r = con.execute(
            "SELECT px, ts_ms FROM prices WHERE symbol=? ORDER BY ts_ms DESC LIMIT 1",
            (str(symbol),),
        ).fetchone()
    if not r:
        return None, None
    try:
        px, px_ts = float(r[0]), int(r[1])
        # Fail if price is too stale (default 5 minutes)
        max_age_ms = int(os.environ.get("BROKER_MAX_PRICE_AGE_MS", "300000"))
        if (ts_ms - px_ts) > max_age_ms:
            return None, None
        return px, px_ts

    except Exception:
        return None, None


def _read_position(con, symbol: str):
    r = con.execute(
        "SELECT qty, avg_px FROM broker_positions WHERE symbol=?",
        (str(symbol),),
    ).fetchone()
    if not r:
        return 0.0, 0.0
    return float(r[0]), float(r[1])


def _write_position(con, symbol: str, qty: float, avg_px: float, ts_ms: int):
    con.execute(
        """
        INSERT INTO broker_positions(symbol, qty, avg_px, updated_ts_ms)
        VALUES(?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
          qty=excluded.qty,
          avg_px=excluded.avg_px,
          updated_ts_ms=excluded.updated_ts_ms
        """,
        (str(symbol), float(qty), float(avg_px), int(ts_ms)),
    )

def _exec_px(
    mid_px: float,
    side: str,
    trade_notional: float = 0.0,
    equity: float = 0.0,
    slip_bps_override: float = None,
    spread_bps_override: float = None,
) -> float:
    """
    Execution price with:
      - spread (half spread added/subtracted)
      - slippage (bps), optionally size-aware using an impact proxy

    Optional per-call overrides:
      - slip_bps_override
      - spread_bps_override
    """
    mid_px = _safe_f(mid_px, 0.0)
    if mid_px <= 0.0:
        return 0.0

    # clamp knobs to sane ranges
    spread_bps = max(0.0, _safe_f(spread_bps_override if spread_bps_override is not None else BROKER_SPREAD_BPS, 0.0))
    slip_bps = max(0.0, _safe_f(slip_bps_override if slip_bps_override is not None else BROKER_SLIPPAGE_BPS, 0.0))
    fee_bps = max(0.0, _safe_f(BROKER_FEE_BPS, 0.0))  # not used here, but kept consistent
    _ = fee_bps

    half_spread = (spread_bps / 10000.0) * mid_px / 2.0
    base_slip = (slip_bps / 10000.0) * mid_px

    # size-aware slippage (impact proxy): increases with notional/equity
    slip = base_slip
    try:
        eq = _safe_f(equity, 0.0)
        tn = abs(_safe_f(trade_notional, 0.0))
        impact_alpha = max(0.0, _safe_f(BROKER_IMPACT_ALPHA, 0.0))
        if eq > 1e-9 and impact_alpha > 0.0 and tn > 0.0:
            slip = base_slip * (1.0 + impact_alpha * (tn / eq))
    except Exception:
        slip = base_slip

    s = str(side or "").upper()
    if s == "BUY":
        return max(0.0, mid_px + half_spread + slip)
    # SELL
    return max(0.0, mid_px - half_spread - slip)


def _fee(notional: float) -> float:
    return abs(float(notional or 0.0)) * (BROKER_FEE_BPS / 10000.0)


def _write_fill(
    con,
    ts_ms: int,
    source_order_id,
    symbol: str,
    qty: float,
    px: float,
    note: str = "",
    explain_json: str = None,
):
    
    con.execute(
        """
        INSERT INTO broker_fills(ts_ms, symbol, qty, px, source_order_id, note, explain_json)
        VALUES(?,?,?,?,?,?,?)
        """,
        (int(ts_ms), str(symbol), float(qty), float(px), source_order_id, str(note or ""), explain_json),
    )

    # --- execution ledger mirror (for slippage + pnl attribution parity) ---
    try:
        from engine.execution_ledger import log_fill

        log_fill(
            client_order_id=f"sim_{int(source_order_id) if source_order_id is not None else 'override'}_{symbol}",
            fill_ts_ms=int(ts_ms),
            fill_qty=float(qty),
            fill_px=float(px),
            fees=None,
            liquidity="sim",
            raw={"note": note},
        )
    except Exception:
        pass


def _mark_to_market(con, ts_ms: int):
    acct = _read_account(con)
    cash = float(acct.get("cash") or 0.0)
    eq = cash

    rows = con.execute("SELECT symbol, qty FROM broker_positions").fetchall()
    for sym, qty in rows or []:
        px, _ = _get_price_at_or_before(con, str(sym), int(ts_ms))
        if px is None:
            continue
        eq += float(qty) * float(px)

    # Safety: prevent runaway negative equity from poisoning downstream sizing
    if not (eq > -1e12):
        eq = -1e12

    _write_account(con, cash=cash, equity=eq, ts_ms=ts_ms)
    con.commit()
    return {"cash": float(cash), "equity": float(eq), "updated_ts_ms": int(ts_ms)}


def _equity(con, ts_ms: int) -> float:
    # capital-aware: prefer stored equity; if missing/zero, mark-to-market
    acct = _read_account(con)
    eq = float(acct.get("equity") or 0.0)
    if eq <= 0.0:
        acct = _mark_to_market(con, ts_ms)
        eq = float(acct.get("equity") or 0.0)
    return float(eq)

def apply_new_portfolio_orders(
    max_rows: int = 500,
    dry_run: bool = False,
    override_orders: List[dict] = None,
    override_order_id: int = None,
    override_ts_ms: int = None,
) -> dict:
    """

    Apply latest portfolio_orders (targets) into broker_positions with realism:
      - spread + slippage via _exec_px
      - fees via _fee
      - max notional cap per apply pass (equity * BROKER_MAX_TRADE_PCT_EQUITY)
      - chunking with latency timestamps
      - cash debits/credits and mark-to-market equity
    """
    con = connect()
    try:
        _ensure_tables(con)

        now_ms = _now_ms()

        if override_orders is not None:
            order_id = int(override_order_id) if override_order_id is not None else None
            ts_ms = int(override_ts_ms) if override_ts_ms is not None else int(now_ms)
            orders = list(override_orders or [])
        else:
            # Read the latest *row-per-order* portfolio_orders batch (no orders_json dependency)
            from engine.portfolio_execution_intents import load_latest_execution_intents

            batch = load_latest_execution_intents(con)
            orders = list(batch.get("intents") or [])
            order_id = batch.get("batch_id")
            ts_ms = int(batch.get("batch_ts_ms") or now_ms)

            if not orders:
                acct = _mark_to_market(con, now_ms)
                return {"ok": True, "status": "no_orders", "account": acct}

        # -----------------------------
        # ALE/EPE integration note:
        # - EPE should already have TTL-filtered these intents before routing.
        # - broker_sim still enforces per-order TTL locally (defense in depth).
        # -----------------------------
        ale_meta = {"ok": True, "note": "ale_applied_upstream_or_ttl_guard_local"}

        # -----------------------------
        # DRY RUN: preview only, no state mutation
        # -----------------------------
        if dry_run:
            return {
                "ok": True,
                "status": "dry_run_preview",
                "order_id": (int(order_id) if order_id is not None else None),
                "orders": orders,
                "ale": ale_meta,
                "account": _read_account(con),
            }

        # execute orders (already shaped upstream by EPE; still TTL-guarded per-order below)
        orders = list(orders or [])


        # idempotency guard: only apply each batch once (skip when order_id is None)
        if order_id is not None:
            last_applied = _get_meta(con, "last_portfolio_orders_id")
            if last_applied is not None:
                try:
                    if int(last_applied) >= int(order_id):
                        acct = _mark_to_market(con, now_ms)
                        return {"ok": True, "status": "already_applied", "order_id": int(order_id), "account": acct}
                except Exception:
                    pass

        acct = _read_account(con)
        cash = float(acct.get("cash") or 0.0)
        equity = float(_equity(con, ts_ms) or 0.0)

        # guard: sizing needs a positive reference; if equity <= 0, do not trade
        if not _is_finite(equity) or equity <= 0.0:
            acct = _mark_to_market(con, now_ms)
            if order_id is not None:
                _set_meta(con, "last_portfolio_orders_id", str(order_id))
                con.commit()
            return {
                "ok": True,
                "status": "skipped_nonpositive_equity",
                "order_id": (int(order_id) if order_id is not None else None),
                "fills_written": 0,
                "account": acct,
            }

        base_max_notional_budget = max(0.0, float(equity) * float(BROKER_MAX_TRADE_PCT_EQUITY))

        # ------------------------------------------------------------
        # Execution ROI conditioning (sizing/execution only; not signal)
        # Uses factor_features + earnings_calendar:
        # - options.skew_25d_z: stressed skew => smaller size + more slippage
        # - flows.index_constituent_imbalance_z: rotation => smaller size + more slippage/latency
        # - earnings proximity: near earnings => smaller size + more slippage
        # ------------------------------------------------------------
        skew_z = _get_factor_feature_asof(con, "options.skew_25d_z", int(ts_ms))
        flow_z = _get_factor_feature_asof(con, "flows.index_constituent_imbalance_z", int(ts_ms))

        stress_mag = max(
            0.0,
            max(
                abs(float(skew_z)) - float(_EXEC_SKEW_Z_THRESH),
                abs(float(flow_z)) - float(_EXEC_FLOW_Z_THRESH),
            ),
        )

        # Global stress sizing multiplier (bounded)
        stress_size_mult = 1.0
        if stress_mag > 0.0:
            stress_size_mult = float(_clamp(1.0 - (stress_mag * float(_EXEC_STRESS_SIZE_MAX_REDUCTION)), 0.20, 1.0))

        # Global stress slippage/latency adds (bounded)
        stress_slip_add_bps = 0.0
        if stress_mag > 0.0:
            stress_slip_add_bps = float(_clamp(stress_mag * float(_EXEC_STRESS_SLIP_ADD_BPS), 0.0, 5.0))

        stress_latency_mult = 1.0
        if abs(float(flow_z)) > float(_EXEC_FLOW_Z_THRESH):
            stress_latency_mult = float(
                _clamp(
                    1.0 + 0.25 * (abs(float(flow_z)) - float(_EXEC_FLOW_Z_THRESH)),
                    1.0,
                    float(_EXEC_STRESS_LATENCY_MULT_MAX),
                )
            )

        max_notional_budget = max(0.0, float(base_max_notional_budget) * float(stress_size_mult))

        # default chunk cap (may be overridden per-order by EPE)
        chunk_cap_notional = max(1e-9, float(max_notional_budget) * float(BROKER_CHUNK_PCT or 0.33))

        wrote_fills = False
        fills_written = 0

        for o in (orders or [])[: int(max_rows)]:
            symbol = str(o.get("symbol") or "").strip()
            order_ttl_ms = int(o.get("alpha_ttl_ms") or 0)

            con.execute(
                """
                INSERT INTO broker_order_state(
                    source_order_id, symbol, state, created_ts_ms, updated_ts_ms, ttl_ms, meta_json
                )
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    int(o.get("source_order_id") or 0),
                    symbol,
                    "PENDING",
                    int(ts_ms),
                    int(ts_ms),
                    order_ttl_ms,
                    json.dumps(o),
                ),
            )
            if not symbol:
                continue

            # Kill switch (global/symbol) is enforced here as a last line of defense
            try:
                from engine.kill_switch import execution_allowed

                allow, _, _ = execution_allowed(con=con, symbol=symbol, regime=None)
                if not allow:
                    continue
            except Exception:
                # Fail-closed on enforcement errors
                continue

            # ------------------------------------------------------------
            # PHASE 4: EPE policy extraction + regime-adaptive microstructure
            # ------------------------------------------------------------
            _epe_ov = o.get("epe_broker_sim_overrides") or {}

            # base knobs (may be overridden per order)
            try:
                _lat_ms = int(_epe_ov.get("latency_ms")) if _epe_ov.get("latency_ms") is not None else None
            except Exception:
                _lat_ms = None
            try:
                _chunk_pct = float(_epe_ov.get("chunk_pct")) if _epe_ov.get("chunk_pct") is not None else None
            except Exception:
                _chunk_pct = None
            try:
                _extra_slip = float(_epe_ov.get("extra_slippage_bps")) if _epe_ov.get("extra_slippage_bps") is not None else 0.0
            except Exception:
                _extra_slip = 0.0

            # EPE policy fields (optional)
            order_type = str(
                o.get("order_type")
                or o.get("epe_order_type")
                or "MARKET"
            ).upper().strip()

            aggressiveness = str(
                o.get("aggressiveness")
                or o.get("epe_aggressiveness")
                or "NEUTRAL"
            ).upper().strip()

            try:
                max_reprice_attempts = int(o.get("max_reprice_attempts") or o.get("epe_max_reprice_attempts") or 0)
            except Exception:
                max_reprice_attempts = 0

            # regime/volatility hints (optional)
            regime = str(o.get("regime") or o.get("epe_regime") or "").upper().strip()
            try:
                volatility = float(o.get("volatility") or o.get("epe_volatility") or 0.0)
            except Exception:
                volatility = 0.0

            # base locals
            local_latency_ms = int(_lat_ms) if (_lat_ms is not None and int(_lat_ms) > 0) else int(BROKER_LATENCY_MS)
            local_chunk_pct = float(_chunk_pct) if (_chunk_pct is not None and 0.01 <= float(_chunk_pct) <= 1.0) else float(BROKER_CHUNK_PCT)

            # global stress conditioning (execution-only)
            try:
                local_latency_ms = int(max(1, int(float(local_latency_ms) * float(stress_latency_mult))))
            except Exception:
                pass
            try:
                if abs(float(skew_z)) > float(_EXEC_SKEW_Z_THRESH) or abs(float(flow_z)) > float(_EXEC_FLOW_Z_THRESH):
                    local_chunk_pct = float(_clamp(local_chunk_pct * 0.85, 0.05, 1.0))
            except Exception:
                pass

            # regime-adaptive tweaks (deterministic; auditable)
            # - higher vol => smaller chunks + more latency (slower fill) + more slippage
            # - "ILLQ"/"LOW_LIQ"/"WIDE" => more slippage + smaller chunks
            vol = max(0.0, float(volatility))
            if vol >= 0.03:
                local_chunk_pct = float(_clamp(local_chunk_pct * 0.60, 0.05, 1.0))
                local_latency_ms = int(max(local_latency_ms, int(BROKER_LATENCY_MS * 2)))
                _extra_slip = float(_extra_slip) + 0.50
            elif vol >= 0.015:
                local_chunk_pct = float(_clamp(local_chunk_pct * 0.80, 0.05, 1.0))
                _extra_slip = float(_extra_slip) + 0.25

            if regime in ("ILLQ", "LOW_LIQ", "WIDE", "WIDE_SPREAD", "THIN"):
                local_chunk_pct = float(_clamp(local_chunk_pct * 0.70, 0.05, 1.0))
                _extra_slip = float(_extra_slip) + 0.75

            # aggressiveness affects effective slippage (more aggressive => more slippage)
            aggr_slip_bps = 0.0
            if aggressiveness == "PASSIVE":
                aggr_slip_bps = -0.25
            elif aggressiveness == "AGGRESSIVE":
                aggr_slip_bps = 0.50

            local_slip_bps = float(BROKER_SLIPPAGE_BPS) + float(_extra_slip) + float(aggr_slip_bps) + float(stress_slip_add_bps)

            # track limit reprice attempts across chunks
            attempts_left = int(max(0, max_reprice_attempts))
            order_type_eff = str(order_type)


            to_side = str(o.get("to_side") or "FLAT").upper()

            to_w = _safe_f(o.get("to_weight"), 0.0)
            if not _is_finite(to_w):
                continue

            px_mid, _ = _get_price_at_or_before(con, symbol, ts_ms)
            if px_mid is None or float(px_mid) <= 0.0:
                continue

            # target qty in units (capital-aware)
            target_qty = (to_w * equity) / float(px_mid)
            if to_side == "SHORT":
                target_qty = -abs(target_qty)
            elif to_side == "LONG":
                target_qty = abs(target_qty)
            else:
                target_qty = 0.0

            cur_qty, cur_avg = _read_position(con, symbol)
            delta = float(target_qty) - float(cur_qty)
            if abs(delta) < 1e-9:
                continue

            # --- execution ledger mirror (ensure metrics + attribution work in sim) ---
            # Create/refresh execution_orders row keyed by the same client_order_id used by _write_fill.
            try:
                from engine.execution_ledger import log_submit

                _extra = dict(o or {})
                try:
                    ex = _extra.get("explain") or {}
                    if isinstance(ex, dict):
                        strat = (ex.get("strategy") or {}) if isinstance(ex.get("strategy"), dict) else {}
                        if strat.get("name"):
                            _extra["strategy_name"] = str(strat.get("name"))
                except Exception:
                    pass

                log_submit(
                    client_order_id=f"sim_{int(o.get('source_order_id') or 0)}_{symbol}",
                    broker="sim",
                    symbol=str(symbol),
                    qty=float(delta),
                    submit_ts_ms=int(ts_ms),
                    ref_px=float(px_mid),
                    broker_order_id=None,
                    portfolio_orders_id=(int(o.get("source_order_id")) if o.get("source_order_id") is not None else None),
                    source_alert_id=(int(o.get("source_alert_id")) if o.get("source_alert_id") is not None else None),
                    extra=_extra,
                )
            except Exception:
                pass

            remaining = float(delta)
            chunk_idx = 0

            # per-order chunk cap (regime/vol adjusted) + earnings proximity conditioning
            earnings_decay = _earnings_proximity_decay(con, symbol, int(ts_ms))
            # size reduction near earnings (bounded)
            earnings_size_mult = float(_clamp(1.0 - (float(earnings_decay) * float(_EXEC_EARNINGS_SIZE_MAX_REDUCTION)), 0.20, 1.0))
            local_max_notional_budget = max(0.0, float(max_notional_budget) * float(earnings_size_mult))
            chunk_cap_notional = max(1e-9, float(local_max_notional_budget) * float(local_chunk_pct or 0.33))

            # slippage add near earnings (execution-only)
            local_slip_bps = float(local_slip_bps) + float(_clamp(float(earnings_decay) * float(_EXEC_EARNINGS_SLIP_ADD_BPS), 0.0, 5.0))

            while abs(remaining) > 1e-9:

                # TTL enforcement
                if order_ttl_ms and (_now_ms() - ts_ms) > order_ttl_ms:
                    con.execute(
                        """
                        UPDATE broker_order_state
                        SET state=?, updated_ts_ms=?
                        WHERE source_order_id=? AND symbol=? AND state='PENDING'
                        """,
                        ("EXPIRED", _now_ms(), int(o.get("source_order_id") or 0), symbol),
                    )
                    break
                if max_notional_budget <= 0.0 or local_max_notional_budget <= 0.0:
                    break

                chunk_side = "BUY" if remaining > 0 else "SELL"

                # Use price at this chunk's simulated fill time (latency-aware), not the parent ts_ms
                fill_ts = int(int(ts_ms) + (int(chunk_idx) * int(local_latency_ms)))

                px_mid_chunk, _ = _get_price_at_or_before(con, symbol, int(fill_ts))
                px_mid_use = px_mid_chunk if (px_mid_chunk is not None and float(px_mid_chunk) > 0.0) else px_mid

                # ------------------------------------------------------------
                # PHASE 4: Order type + aggressiveness shaping (MARKET vs LIMIT)
                # - MARKET: uses _exec_px with (possibly adjusted) slippage
                # - LIMIT: improved price but partial fills; cancel/replace escalates to MARKET
                # ------------------------------------------------------------
                px_exec = 0.0

                # provisional px for sizing (MARKET-like baseline)
                px_mkt = _exec_px(
                    px_mid_use,
                    chunk_side,
                    trade_notional=0.0,
                    equity=equity,
                    slip_bps_override=float(local_slip_bps),
                )
                if px_mkt <= 0.0:
                    break

                # cap by remaining and notional budget using provisional px
                effective_budget = float(min(float(local_max_notional_budget), float(max_notional_budget)))

                remaining_notional = abs(remaining) * px_mkt
                if remaining_notional > effective_budget:
                    qty_cap = (effective_budget / px_mkt) * (1.0 if remaining > 0 else -1.0)
                else:
                    qty_cap = remaining

                # chunk cap using provisional px
                if abs(qty_cap) * px_mkt > chunk_cap_notional:
                    qty_cap = (chunk_cap_notional / px_mkt) * (1.0 if remaining > 0 else -1.0)

                if abs(qty_cap) < 1e-9:
                    break

                # Choose effective order type for this chunk
                if order_type_eff == "LIMIT":
                    # LIMIT improves price relative to market baseline.
                    # PASSIVE => better price, lower fill; AGGRESSIVE => closer to market, higher fill.
                    improve = 0.5
                    if aggressiveness == "PASSIVE":
                        improve = 1.0
                    elif aggressiveness == "AGGRESSIVE":
                        improve = 0.15

                    # allow cancel/replace: each attempt reduces improvement (more aggressive repricing)
                    if attempts_left > 0:
                        step = min(max_reprice_attempts, max(0, max_reprice_attempts - attempts_left))
                        improve = float(_clamp(improve - 0.25 * float(step), 0.0, 1.0))

                    half_spread = (float(BROKER_SPREAD_BPS) / 10000.0) * float(px_mid_use) / 2.0
                    if chunk_side == "BUY":
                        px_exec = max(0.0, float(px_mid_use) - (half_spread * float(improve)))
                    else:
                        px_exec = max(0.0, float(px_mid_use) + (half_spread * float(improve)))

                    # deterministic partial fill model
                    base_fill = 0.70
                    if aggressiveness == "PASSIVE":
                        base_fill = 0.45
                    elif aggressiveness == "AGGRESSIVE":
                        base_fill = 0.95

                    # higher vol / illiq => lower fill
                    fill_penalty = float(_clamp(vol * 6.0, 0.0, 0.50))
                    fill_frac = float(_clamp(base_fill - fill_penalty, 0.20, 1.0))

                    # deterministic per-chunk variation (auditable, reproducible)
                    u = _u01(f"{order_id}|{symbol}|{chunk_idx}|{fill_ts}|{order_type_eff}|{aggressiveness}")
                    jitter = float(_clamp((u - 0.5) * 0.10, -0.05, 0.05))
                    fill_frac = float(_clamp(fill_frac + jitter, 0.20, 1.0))

                    # apply partial fill
                    qty_cap = float(qty_cap) * float(fill_frac)

                    if abs(qty_cap) < 1e-9:
                        # no fill at this price => cancel/replace attempt
                        if attempts_left > 0:
                            attempts_left -= 1
                            chunk_idx += 1
                            # simulate time passing, but keep remaining unchanged
                            continue
                        # escalate remainder to MARKET
                        order_type_eff = "MARKET"
                        continue

                    # if we didn't fill the whole remainder, burn an attempt (cancel/replace)
                    if abs(qty_cap) < abs(remaining) and attempts_left > 0:
                        attempts_left -= 1
                        if attempts_left <= 0:
                            # no more reprices => escalate to MARKET for remaining
                            order_type_eff = "MARKET"

                else:
                    # MARKET path: recompute with size-aware slippage
                    notional_est = float(qty_cap) * float(px_mid_use)
                    px_exec = _exec_px(
                        px_mid_use,
                        chunk_side,
                        trade_notional=notional_est,
                        equity=equity,
                        slip_bps_override=float(local_slip_bps),
                    )

                if px_exec <= 0.0:
                    break

                # execute
                notional = float(qty_cap) * float(px_exec)
                fee = _fee(notional)

                # Optional no-margin mode: prevent cash from going negative on buys
                if (not BROKER_ALLOW_MARGIN) and (notional > 0.0):
                    max_afford = max(0.0, float(cash) - float(fee))
                    if max_afford <= 0.0:
                        break
                    max_qty = max_afford / float(px_exec)
                    if max_qty < abs(float(qty_cap)):
                        qty_cap = (max_qty * (1.0 if qty_cap > 0 else -1.0))
                        if abs(qty_cap) < 1e-9:
                            break
                        notional = float(qty_cap) * float(px_exec)
                        fee = _fee(notional)

                # cash: BUY spends (negative), SELL receives (positive). fees always reduce cash.
                cash += (-notional - fee)

                new_qty = float(cur_qty) + float(qty_cap)

                # avg_px update:
                if (
                    float(cur_qty) == 0.0
                    or (float(cur_qty) > 0 and float(qty_cap) > 0)
                    or (float(cur_qty) < 0 and float(qty_cap) < 0)
                ):
                    old_notional_abs = abs(float(cur_qty)) * float(cur_avg)
                    add_notional_abs = abs(float(qty_cap)) * float(px_exec)
                    denom = abs(float(cur_qty)) + abs(float(qty_cap))
                    new_avg = (old_notional_abs + add_notional_abs) / denom if denom > 1e-12 else float(px_exec)
                else:
                    if (float(cur_qty) > 0 and new_qty > 0) or (float(cur_qty) < 0 and new_qty < 0):
                        new_avg = float(cur_avg)
                    elif abs(new_qty) < 1e-12:
                        new_avg = 0.0
                    else:
                        new_avg = float(px_exec)

                fill_ts = int(fill_ts)

                _write_position(con, symbol, qty=float(new_qty), avg_px=float(new_avg), ts_ms=fill_ts)

                explain = {
                    "mid_px": float(px_mid_use),
                    "exec_px": float(px_exec),
                    "side": str(chunk_side),

                    "order_type": str(order_type_eff),
                    "aggressiveness": str(aggressiveness),
                    "regime": str(regime),
                    "volatility": float(vol),

                    # --- execution ROI conditioning ---
                    "exec_stress": {
                        "skew_z": float(skew_z),
                        "flow_z": float(flow_z),
                        "stress_size_mult": float(stress_size_mult),
                        "stress_slip_add_bps": float(stress_slip_add_bps),
                        "stress_latency_mult": float(stress_latency_mult),
                        "earnings_decay": float(earnings_decay),
                    },

                    "spread_bps": float(BROKER_SPREAD_BPS),
                    "slippage_bps": float(local_slip_bps),
                    "impact_alpha": float(BROKER_IMPACT_ALPHA),
                    "fee_bps": float(BROKER_FEE_BPS),

                    "qty": float(qty_cap),
                    "notional": float(notional),
                    "fee": float(fee),
                    "equity_ref": float(equity),

                    "latency_ms": int(local_latency_ms),
                    "chunk_pct": float(local_chunk_pct),

                    "max_reprice_attempts": int(max_reprice_attempts),
                    "attempts_left": int(attempts_left),

                    "chunk_idx": int(chunk_idx),
                }

                _write_fill(
                    con,
                    ts_ms=fill_ts,
                    source_order_id=order_id,
                    symbol=symbol,
                    qty=float(qty_cap),
                    px=float(px_exec),
                    note=f"spread_bps={BROKER_SPREAD_BPS} slippage_bps={float(local_slip_bps)} fee_bps={BROKER_FEE_BPS}",
                    explain_json=json.dumps(explain),
                )

                # best-effort execution labels (if the table exists)
                try:
                    con.execute(
                        """
                        INSERT OR REPLACE INTO labels_exec (
                          event_id,
                          symbol,
                          horizon_s,
                          ts_ms,
                          source,
                          realized,
                          side,
                          gross_ret,
                          net_ret,
                          mid_in,
                          mid_out,
                          spread_in,
                          fees_bps,
                          slippage_bps,
                          spread_bps,
                          total_cost_bps,
                          extra_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(o.get("event_id") or 0),
                            symbol,
                            int(o.get("horizon_s") or 0),
                            int(fill_ts),
                            "broker_sim",
                            1,
                            1 if qty_cap > 0 else -1,
                            0.0,
                            0.0,
                            float(px_mid_use),
                            float(px_exec),
                            float(BROKER_SPREAD_BPS),
                            float(BROKER_FEE_BPS),
                            float(local_slip_bps),
                            float(BROKER_SPREAD_BPS),
                            float(BROKER_FEE_BPS + float(local_slip_bps) + BROKER_SPREAD_BPS),
                            json.dumps(explain),
                        ),
                    )
                except Exception:
                    pass

                con.execute(
                    """
                    UPDATE broker_order_state
                    SET state=?, updated_ts_ms=?
                    WHERE source_order_id=? AND symbol=? AND state='PENDING'
                    """,
                    ("FILLED", _now_ms(), int(o.get("source_order_id") or 0), symbol),
                )

                wrote_fills = True
                fills_written += 1
                chunk_idx += 1

                # Optional wall-clock latency simulation (default off)
                if BROKER_LATENCY_SLEEP and int(local_latency_ms) > 0:
                    try:
                        time.sleep(max(0.0, int(local_latency_ms) / 1000.0))
                    except Exception:
                        pass

                # update loop state
                cur_qty = float(new_qty)
                cur_avg = float(new_avg)
                remaining -= float(qty_cap)

                max_notional_budget = max(0.0, float(max_notional_budget) - abs(float(notional)))

        # persist cash and MTM equity
        cash = _safe_f(cash, 0.0)

        con.execute(
            "UPDATE broker_account SET cash=?, updated_ts_ms=? WHERE id=1",
            (float(cash), int(now_ms)),
        )
        con.commit()

        acct2 = _mark_to_market(con, int(now_ms))

        # mark orders applied (idempotency)
        if order_id is not None:
            _set_meta(con, "last_portfolio_orders_id", str(order_id))
            con.commit()

        return {
            "ok": True,
            "broker": "sim",
            "status": "applied" if wrote_fills else "no_changes",
            "order_id": (int(order_id) if order_id is not None else None),
            "fills_written": int(fills_written),
            "account": acct2,
        }
    finally:
        con.close()


def broker_equity_at(ts_ms: int, include_prices: bool = False) -> dict:
    """
    Mark-to-market broker equity at an arbitrary timestamp WITHOUT mutating broker_account.

    Returns:
      {
        ok, ts_ms, cash, equity,
        positions: [{symbol, qty, px, px_ts_ms, notional}],
        missing_prices: [symbol...]
      }
    """
    init_broker_db()
    con = connect()
    try:
        acct = _read_account(con)
        cash = float(acct.get("cash") or 0.0)

        eq = float(cash)
        out_positions = []
        missing = []

        rows = con.execute("SELECT symbol, qty FROM broker_positions ORDER BY symbol").fetchall()
        for sym, qty in rows or []:
            sym = str(sym)
            qty = float(qty or 0.0)

            px, px_ts = _get_price_at_or_before(con, sym, int(ts_ms))
            if px is None or float(px) <= 0.0:
                missing.append(sym)
                continue

            notional = float(qty) * float(px)
            eq += notional

            if include_prices:
                out_positions.append(
                    {
                        "symbol": sym,
                        "qty": float(qty),
                        "px": float(px),
                        "px_ts_ms": (int(px_ts) if px_ts is not None else None),
                        "notional": float(notional),
                    }
                )

        return {
            "ok": True,
            "ts_ms": int(ts_ms),
            "cash": float(cash),
            "equity": float(eq),
            "positions": out_positions,
            "missing_prices": missing,
        }
    finally:
        con.close()


def broker_snapshot(limit_fills: int = 50):

    init_broker_db()
    con = connect()
    try:
        acct = con.execute("SELECT cash, equity, updated_ts_ms FROM broker_account WHERE id=1").fetchone()
        cash, equity, upd = (acct[0], acct[1], acct[2]) if acct else (0.0, 1.0, 0)

        pos = con.execute(
            "SELECT symbol, qty, avg_px, updated_ts_ms FROM broker_positions ORDER BY symbol"
        ).fetchall()

        fills = con.execute(
            """
            SELECT ts_ms, symbol, qty, px, source_order_id, note
            FROM broker_fills
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (int(max(1, min(500, int(limit_fills)))),),
        ).fetchall()

        return {
            "ok": True,
            "account": {"cash": float(cash), "equity": float(equity), "updated_ts_ms": int(upd)},
            "positions": [
                {"symbol": r[0], "qty": float(r[1]), "avg_px": float(r[2]), "updated_ts_ms": int(r[3])}
                for r in (pos or [])
            ],
            "fills": [
                {
                    "ts_ms": int(r[0]),
                    "symbol": r[1],
                    "qty": float(r[2]),
                    "px": float(r[3]),
                    "order_id": (int(r[4]) if r[4] is not None else None),
                    "note": r[5],
                }
                for r in (fills or [])
            ],
        }
    finally:
        con.close()


def main():
    res = apply_new_portfolio_orders()
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
