# FILE: dev_core/position_reconcile.py
# NEW FILE (CREATE)

"""
Pre-Live Position Reconciliation Gate

Purpose:
- Before sending LIVE orders, reconcile broker positions vs a stored baseline.
- If mismatch exceeds tolerances, trip kill-switch (global) and block execution.

Env:
  EXECUTION_PRELIVE_RECONCILE=1              (default 1)
  EXECUTION_RECONCILE_REQUIRE_BASELINE=1     (default 1)  -> if no baseline, block unless allow bootstrap
  EXECUTION_RECONCILE_ALLOW_BOOTSTRAP=0      (default 0)  -> if 1 and no baseline, create baseline and allow once

Tolerances:
  POSITION_RECONCILE_QTY_TOL=0.01            (absolute qty tolerance per symbol)
  POSITION_RECONCILE_IGNORE_QTY_LT=0.001     (ignore tiny positions)
  POSITION_RECONCILE_MAX_MISMATCHED=0        (max mismatched symbols allowed)
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from engine.runtime.storage import connect, init_db
from engine.kill_switch import set_kill_switch


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_schema(con) -> None:
    con.executescript(
        """
CREATE TABLE IF NOT EXISTS position_reconcile_baseline (
  broker TEXT PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  positions_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_reconcile_audit (
  ts_ms INTEGER NOT NULL,
  broker TEXT NOT NULL,
  ok INTEGER NOT NULL,
  status TEXT NOT NULL,
  mismatched_n INTEGER NOT NULL,
  max_abs_qty_diff REAL NOT NULL,
  total_abs_qty_diff REAL NOT NULL,
  detail_json TEXT,
  PRIMARY KEY (ts_ms, broker)
);

CREATE INDEX IF NOT EXISTS idx_position_reconcile_audit_ts
  ON position_reconcile_audit(ts_ms);
"""
    )
    try:
        con.commit()
    except Exception:
        pass


def _safe_f(x, d: float = 0.0) -> float:
    try:
        v = float(x)
        if v == v:
            return float(v)
    except Exception:
        pass
    return float(d)


def _norm_positions(positions: List[Dict[str, Any]], ignore_lt: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for p in positions or []:
        try:
            sym = str(p.get("symbol") or "").strip().upper()
            if not sym:
                continue
            qty = _safe_f(p.get("qty"), 0.0)
            if abs(qty) < float(ignore_lt):
                continue
            out[sym] = float(qty)
        except Exception:
            continue
    return out


def _load_baseline(con, broker: str) -> Optional[Dict[str, float]]:
    r = con.execute(
        "SELECT positions_json FROM position_reconcile_baseline WHERE broker=?",
        (str(broker),),
    ).fetchone()
    if not r:
        return None
    try:
        raw = json.loads(r[0] or "{}")
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        return None
    out: Dict[str, float] = {}
    for k, v in raw.items():
        try:
            sym = str(k).strip().upper()
            qty = _safe_f(v, 0.0)
            out[sym] = float(qty)
        except Exception:
            continue
    return out


def _save_baseline(con, broker: str, ts_ms: int, pos_map: Dict[str, float]) -> None:
    con.execute(
        """
        INSERT INTO position_reconcile_baseline(broker, ts_ms, positions_json)
        VALUES(?,?,?)
        ON CONFLICT(broker) DO UPDATE SET
          ts_ms=excluded.ts_ms,
          positions_json=excluded.positions_json
        """,
        (
            str(broker),
            int(ts_ms),
            json.dumps(pos_map or {}, separators=(",", ":"), sort_keys=True),
        ),
    )
    try:
        con.commit()
    except Exception:
        pass


def _broker_positions(broker: str) -> Tuple[bool, str, List[Dict[str, Any]]]:
    b = str(broker or "").lower().strip()

    if b in ("alpaca", "alpaca_rest"):
        try:
            from engine.broker_alpaca_rest import get_positions
            res = get_positions() or []
            out = [{"symbol": str(x.get("symbol") or "").upper(), "qty": float(x.get("qty") or x.get("quantity") or x.get("qty_available") or x.get("qty_long") or x.get("qty_short") or x.get("qty", 0) or 0.0)} for x in []]  # never used
            # Normalize Alpaca format
            norm = []
            for x in (res or []):
                sym = str(x.get("symbol") or "").upper().strip()
                if not sym:
                    continue
                try:
                    q = float(x.get("qty") or 0.0)
                except Exception:
                    q = 0.0
                norm.append({"symbol": sym, "qty": q})
            return True, "ok", norm
        except Exception as e:
            return False, f"alpaca_positions_error:{e}", []

    if b in ("ibkr", "interactivebrokers", "interactive_brokers", "ib_gateway", "ibgateway", "tws"):
        try:
            from engine.broker_ibkr_gateway import get_positions_live
            res = get_positions_live() or []
            return True, "ok", list(res or [])
        except Exception as e:
            return False, f"ibkr_positions_error:{e}", []

    if b in ("sim", "paper", "sandbox"):
        # Optional: best-effort from broker_sim tables
        try:
            con = connect()
            try:
                rows = con.execute(
                    "SELECT symbol, qty FROM broker_positions"
                ).fetchall() or []
                out = [{"symbol": str(r[0]).upper().strip(), "qty": _safe_f(r[1], 0.0)} for r in rows if r and r[0]]
                return True, "ok", out
            finally:
                con.close()
        except Exception as e:
            return False, f"sim_positions_error:{e}", []

    return False, "unknown_broker_for_positions", []


def pre_live_position_reconcile(
    broker: str,
    *,
    con=None,
) -> Dict[str, Any]:
    """
    Returns dict:
      { ok, status, broker, mismatched_n, max_abs_qty_diff, total_abs_qty_diff, detail, fatal_reconcile }
    """
    enabled = os.environ.get("EXECUTION_PRELIVE_RECONCILE", "1") == "1"
    if not enabled:
        return {"ok": True, "status": "skipped_disabled", "broker": str(broker), "fatal_reconcile": False}

    require_baseline = os.environ.get("EXECUTION_RECONCILE_REQUIRE_BASELINE", "1") == "1"
    allow_bootstrap = os.environ.get("EXECUTION_RECONCILE_ALLOW_BOOTSTRAP", "0") == "1"

    qty_tol = float(os.environ.get("POSITION_RECONCILE_QTY_TOL", "0.01"))
    ignore_lt = float(os.environ.get("POSITION_RECONCILE_IGNORE_QTY_LT", "0.001"))
    max_mismatched = int(os.environ.get("POSITION_RECONCILE_MAX_MISMATCHED", "0"))

    owns = False
    if con is None:
        init_db()
        con = connect()
        owns = True

    ts_ms = _now_ms()

    try:
        _ensure_schema(con)

        ok_b, bstatus, broker_pos = _broker_positions(str(broker))
        if not ok_b:
            detail = {"error": bstatus}
            con.execute(
                """
                INSERT OR REPLACE INTO position_reconcile_audit
                (ts_ms, broker, ok, status, mismatched_n, max_abs_qty_diff, total_abs_qty_diff, detail_json)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (int(ts_ms), str(broker), 0, "positions_fetch_failed", 0, 0.0, 0.0, json.dumps(detail)),
            )
            try:
                con.commit()
            except Exception:
                pass
            return {
                "ok": False,
                "status": "positions_fetch_failed",
                "broker": str(broker),
                "detail": detail,
                "fatal_reconcile": True,
            }

        bmap = _norm_positions(broker_pos, ignore_lt=ignore_lt)

        baseline = _load_baseline(con, str(broker))
        if baseline is None:
            if allow_bootstrap:
                _save_baseline(con, str(broker), ts_ms, bmap)
                detail = {"status": "baseline_bootstrapped", "n": int(len(bmap))}
                con.execute(
                    """
                    INSERT OR REPLACE INTO position_reconcile_audit
                    (ts_ms, broker, ok, status, mismatched_n, max_abs_qty_diff, total_abs_qty_diff, detail_json)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (int(ts_ms), str(broker), 1, "baseline_bootstrapped", 0, 0.0, 0.0, json.dumps(detail)),
                )
                try:
                    con.commit()
                except Exception:
                    pass
                return {
                    "ok": True,
                    "status": "baseline_bootstrapped",
                    "broker": str(broker),
                    "mismatched_n": 0,
                    "max_abs_qty_diff": 0.0,
                    "total_abs_qty_diff": 0.0,
                    "detail": detail,
                    "fatal_reconcile": False,
                }

            if require_baseline:
                detail = {"error": "baseline_missing", "require_baseline": True, "allow_bootstrap": False}
                con.execute(
                    """
                    INSERT OR REPLACE INTO position_reconcile_audit
                    (ts_ms, broker, ok, status, mismatched_n, max_abs_qty_diff, total_abs_qty_diff, detail_json)
                    VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (int(ts_ms), str(broker), 0, "baseline_missing", 0, 0.0, 0.0, json.dumps(detail)),
                )
                try:
                    con.commit()
                except Exception:
                    pass
                return {
                    "ok": False,
                    "status": "baseline_missing",
                    "broker": str(broker),
                    "detail": detail,
                    "fatal_reconcile": True,
                }

            # Not required → treat as ok, and write baseline for future.
            _save_baseline(con, str(broker), ts_ms, bmap)
            return {"ok": True, "status": "baseline_created", "broker": str(broker), "fatal_reconcile": False}

        # Compare
        keys = set(bmap.keys()) | set(baseline.keys())
        mismatched = []
        total_abs = 0.0
        max_abs = 0.0

        for sym in sorted(keys):
            bq = _safe_f(bmap.get(sym), 0.0)
            eq = _safe_f(baseline.get(sym), 0.0)
            d = float(bq - eq)
            ad = abs(d)
            if ad <= float(qty_tol):
                continue
            mismatched.append({"symbol": sym, "broker_qty": bq, "expected_qty": eq, "diff_qty": d})
            total_abs += float(ad)
            if ad > max_abs:
                max_abs = float(ad)

        mismatched_n = int(len(mismatched))
        ok = (mismatched_n <= int(max_mismatched))

        status = "ok" if ok else "mismatch"
        detail = {
            "mismatched": mismatched[:50],  # cap
            "mismatched_n": mismatched_n,
            "qty_tol": float(qty_tol),
            "ignore_lt": float(ignore_lt),
            "max_mismatched": int(max_mismatched),
        }

        con.execute(
            """
            INSERT OR REPLACE INTO position_reconcile_audit
            (ts_ms, broker, ok, status, mismatched_n, max_abs_qty_diff, total_abs_qty_diff, detail_json)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                int(ts_ms),
                str(broker),
                1 if ok else 0,
                str(status),
                int(mismatched_n),
                float(max_abs),
                float(total_abs),
                json.dumps(detail, separators=(",", ":"), sort_keys=True),
            ),
        )
        try:
            con.commit()
        except Exception:
            pass

        if ok:
            return {
                "ok": True,
                "status": "ok",
                "broker": str(broker),
                "mismatched_n": mismatched_n,
                "max_abs_qty_diff": float(max_abs),
                "total_abs_qty_diff": float(total_abs),
                "detail": detail,
                "fatal_reconcile": False,
            }

        # Mismatch → trip kill switch (global) and block.
        try:
            set_kill_switch(
                scope="global",
                key="global",
                enabled=1,
                reason="prelive_position_mismatch",
                actor="position_reconcile",
                meta={
                    "broker": str(broker),
                    "mismatched_n": mismatched_n,
                    "max_abs_qty_diff": float(max_abs),
                    "total_abs_qty_diff": float(total_abs),
                    "qty_tol": float(qty_tol),
                },
                action="TRIP",
                con=con,
            )
        except Exception:
            pass

        return {
            "ok": False,
            "status": "mismatch",
            "broker": str(broker),
            "mismatched_n": mismatched_n,
            "max_abs_qty_diff": float(max_abs),
            "total_abs_qty_diff": float(total_abs),
            "detail": detail,
            "fatal_reconcile": True,
        }

    finally:
        if owns:
            try:
                con.close()
            except Exception:
                pass
