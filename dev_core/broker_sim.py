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
from dev_core.storage import connect

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

# -----------------------------
# Broker realism knobs (env)
# -----------------------------
BROKER_SPREAD_BPS = float(os.environ.get("BROKER_SPREAD_BPS", "2.0"))          # total spread (bps)
BROKER_SLIPPAGE_BPS = float(os.environ.get("BROKER_SLIPPAGE_BPS", "1.0"))      # extra slippage (bps)
BROKER_FEE_BPS = float(os.environ.get("BROKER_FEE_BPS", "0.5"))                # commission/fees (bps of notional)
BROKER_MAX_TRADE_PCT_EQUITY = float(os.environ.get("BROKER_MAX_TRADE_PCT_EQUITY", "0.35"))  # cap per apply pass
BROKER_CHUNK_PCT = float(os.environ.get("BROKER_CHUNK_PCT", "0.33"))           # split into chunks
BROKER_LATENCY_MS = int(os.environ.get("BROKER_LATENCY_MS", "120"))            # per chunk latency

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

        ok = 0 if had_error else 1

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
    r = con.execute(
        """
        SELECT px, ts_ms
        FROM prices
        WHERE symbol = ? AND ts_ms <= ?
        ORDER BY ts_ms DESC
        LIMIT 1
        """,
        (str(symbol), int(ts_ms)),
    ).fetchone()
    if not r:
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


def _exec_px(mid_px: float, side: str, trade_notional: float = 0.0, equity: float = 0.0) -> float:
    """
    Execution price with:
      - spread (half spread added/subtracted)
      - slippage (bps), optionally size-aware using an impact proxy
    """
    mid_px = _safe_f(mid_px, 0.0)
    if mid_px <= 0.0:
        return 0.0

    # clamp knobs to sane ranges
    spread_bps = max(0.0, _safe_f(BROKER_SPREAD_BPS, 0.0))
    slip_bps = max(0.0, _safe_f(BROKER_SLIPPAGE_BPS, 0.0))
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


def _write_fill(con, ts_ms: int, source_order_id, symbol: str, qty: float, px: float, note: str = "", explain_json: str = None):
    con.execute(
        """
        INSERT INTO broker_fills(ts_ms, symbol, qty, px, source_order_id, note, explain_json)
        VALUES(?,?,?,?,?,?,?)
        """,
        (int(ts_ms), str(symbol), float(qty), float(px), source_order_id, str(note or ""), explain_json),
    )

    # --- execution ledger mirror (for slippage + pnl attribution parity) ---
    try:
        from dev_core.execution_ledger import log_fill
        log_fill(
            client_order_id=f"sim_{int(source_order_id)}_{symbol}",
            fill_ts_ms=int(ts_ms),
            fill_qty=float(qty),
            fill_px=float(px),
            fees=None,
            liquidity="sim",
            raw={"note": note},
        )
    except Exception:
        pass

        (int(ts_ms), str(symbol), float(qty), float(px), source_order_id, str(note or ""), explain_json),
    
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


def apply_new_portfolio_orders(max_rows: int = 500, dry_run: bool = False) -> dict:
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

        row = con.execute(
            "SELECT id, ts_ms, orders_json FROM portfolio_orders ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            acct = _mark_to_market(con, now_ms)
            return {"ok": True, "status": "no_orders", "account": acct}

        order_id = int(row[0])
        ts_ms = int(row[1] or now_ms)
        orders = json.loads(row[2] or "[]") if row[2] else []

        # -----------------------------
        # DRY RUN: preview only, no state mutation
        # -----------------------------
        if dry_run:
            return {
                "ok": True,
                "status": "dry_run_preview",
                "order_id": int(order_id),
                "orders": orders,
                "account": _read_account(con),
            }

        # idempotency guard: only apply each portfolio_orders row once
        last_applied = _get_meta(con, "last_portfolio_orders_id")
        if last_applied is not None:
            try:
                if int(last_applied) >= int(order_id):
                    acct = _mark_to_market(con, now_ms)
                    return {"ok": True, "status": "already_applied", "order_id": order_id, "account": acct}
            except Exception:
                pass

        acct = _read_account(con)
        cash = float(acct.get("cash") or 0.0)
        equity = float(_equity(con, ts_ms) or 0.0)
        # guard: sizing needs a positive reference; if equity <= 0, do not trade
        if not _is_finite(equity) or equity <= 0.0:
            acct = _mark_to_market(con, now_ms)
            _set_meta(con, "last_portfolio_orders_id", str(order_id))
            con.commit()
            return {
                "ok": True,
                "status": "skipped_nonpositive_equity",
                "order_id": int(order_id),
                "fills_written": 0,
                "account": acct,
            }

        max_notional_budget = max(0.0, float(equity) * float(BROKER_MAX_TRADE_PCT_EQUITY))
        chunk_cap_notional = max(1e-9, float(max_notional_budget) * float(BROKER_CHUNK_PCT or 0.33))

        wrote_fills = False
        fills_written = 0

        for o in (orders or [])[: int(max_rows)]:
            symbol = str(o.get("symbol") or "").strip()
            if not symbol:
                continue

            # Kill switch (global/symbol) is enforced here as a last line of defense
            try:
                from dev_core.kill_switch import execution_allowed
                allow, _, _ = execution_allowed(con=con, symbol=symbol, regime=None)
                if not allow:
                    continue
            except Exception:
                # Fail-closed on enforcement errors
                continue

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

            remaining = float(delta)
            chunk_idx = 0

            while abs(remaining) > 1e-9:
                if max_notional_budget <= 0.0:
                    break

                chunk_side = "BUY" if remaining > 0 else "SELL"

                # Use price at this chunk's simulated fill time (latency-aware), not the parent ts_ms
                fill_ts = int(int(ts_ms) + (int(chunk_idx) * int(BROKER_LATENCY_MS)))
                px_mid_chunk, _ = _get_price_at_or_before(con, symbol, int(fill_ts))
                px_mid_use = px_mid_chunk if (px_mid_chunk is not None and float(px_mid_chunk) > 0.0) else px_mid

                # We don't yet know exact chunk qty here; compute a provisional exec px using max possible notional later.
                # We'll recompute after qty_cap is known.
                px_exec = _exec_px(px_mid_use, chunk_side, trade_notional=0.0, equity=equity)
                if px_exec <= 0.0:
                    break


                # cap by remaining and notional budget
                remaining_notional = abs(remaining) * px_exec
                if remaining_notional > max_notional_budget:
                    qty_cap = (max_notional_budget / px_exec) * (1.0 if remaining > 0 else -1.0)
                else:
                    qty_cap = remaining

                # chunk cap
                if abs(qty_cap) * px_exec > chunk_cap_notional:
                    qty_cap = (chunk_cap_notional / px_exec) * (1.0 if remaining > 0 else -1.0)

                if abs(qty_cap) < 1e-9:
                    break

                # Recompute exec px now that qty_cap is known (size-aware slippage)
                notional_est = float(qty_cap) * float(px_mid_use)
                px_exec = _exec_px(px_mid_use, chunk_side, trade_notional=notional_est, equity=equity)
                if px_exec <= 0.0:
                    break

                # execute
                notional = float(qty_cap) * float(px_exec)
                fee = _fee(notional)

                # Optional no-margin mode: prevent cash from going negative on buys
                if (not BROKER_ALLOW_MARGIN) and (notional > 0.0):
                    # max affordable qty given current cash after fees
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
                # - if increasing same direction, blend
                # - if reducing or flipping, keep avg if still same sign; reset if cross 0
                if float(cur_qty) == 0.0 or (float(cur_qty) > 0 and float(qty_cap) > 0) or (float(cur_qty) < 0 and float(qty_cap) < 0):
                    # blend on add
                    old_notional_abs = abs(float(cur_qty)) * float(cur_avg)
                    add_notional_abs = abs(float(qty_cap)) * float(px_exec)
                    denom = abs(float(cur_qty)) + abs(float(qty_cap))
                    new_avg = (old_notional_abs + add_notional_abs) / denom if denom > 1e-12 else float(px_exec)
                else:
                    # reducing or flipping
                    if (float(cur_qty) > 0 and new_qty > 0) or (float(cur_qty) < 0 and new_qty < 0):
                        new_avg = float(cur_avg)
                    elif abs(new_qty) < 1e-12:
                        new_avg = 0.0
                    else:
                        new_avg = float(px_exec)

                # fill_ts already computed for this chunk (do not recompute)
                fill_ts = int(fill_ts)

                _write_position(con, symbol, qty=float(new_qty), avg_px=float(new_avg), ts_ms=fill_ts)
                explain = {
                    "mid_px": float(px_mid_use),
                    "exec_px": float(px_exec),
                    "side": str(chunk_side),
                    "spread_bps": float(BROKER_SPREAD_BPS),
                    "slippage_bps": float(BROKER_SLIPPAGE_BPS),
                    "impact_alpha": float(BROKER_IMPACT_ALPHA),
                    "fee_bps": float(BROKER_FEE_BPS),
                    "qty": float(qty_cap),
                    "notional": float(notional),
                    "fee": float(fee),
                    "equity_ref": float(equity),
                    "latency_ms": int(BROKER_LATENCY_MS),
                    "chunk_idx": int(chunk_idx),
                }

                _write_fill(
                    con,
                    ts_ms=fill_ts,
                    source_order_id=order_id,
                    symbol=symbol,
                    qty=float(qty_cap),
                    px=float(px_exec),
                    note=f"spread_bps={BROKER_SPREAD_BPS} slippage_bps={BROKER_SLIPPAGE_BPS} fee_bps={BROKER_FEE_BPS}",
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
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            float(BROKER_SLIPPAGE_BPS),
                            float(BROKER_SPREAD_BPS),
                            float(BROKER_FEE_BPS + BROKER_SLIPPAGE_BPS + BROKER_SPREAD_BPS),
                            json.dumps(explain),
                        ),
                    )
                except Exception:
                    pass


                wrote_fills = True
                fills_written += 1

                chunk_idx += 1

                # Optional wall-clock latency simulation (default off)
                if BROKER_LATENCY_SLEEP and int(BROKER_LATENCY_MS) > 0:
                    try:
                        time.sleep(max(0.0, int(BROKER_LATENCY_MS) / 1000.0))
                    except Exception:
                        pass

                # update loop state
                cur_qty = float(new_qty)
                cur_avg = float(new_avg)
                remaining -= float(qty_cap)

                max_notional_budget = max(0.0, float(max_notional_budget) - abs(float(notional)))

        # persist cash and MTM equity
        # (write cash first, then mark-to-market equity)
        cash = _safe_f(cash, 0.0)
        ok = 0 if had_error else 1

        con.execute(
            "UPDATE broker_account SET cash=?, updated_ts_ms=? WHERE id=1",
            (float(cash), int(now_ms)),
        )
        con.commit()

        acct2 = _mark_to_market(con, int(now_ms))

        # mark orders applied (idempotency)
        _set_meta(con, "last_portfolio_orders_id", str(order_id))
        con.commit()

        return {
            "ok": True,
            "status": "applied" if wrote_fills else "no_changes",
            "order_id": int(order_id),
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
