# prod_preflight.py
"""Production preflight + smoke cycle.

Runs (best-effort, fail-fast on hard errors):
  1) py_compile integrity check for key modules
  2) core schema init + portfolio backtest schema
  3) optional auto-fix equivalent (idempotent)
  4) simulated execution cycle (labels/size/backtest/rebalance/broker_sim)
  5) execution-cost gating sanity check (if enabled)

Exit codes:
  0 = ok
  2 = warnings only
  3 = hard failure
"""
from __future__ import annotations

import argparse
import json
import os
import py_compile
import subprocess
import sys
import time
from typing import Dict, List, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))

KEY_FILES = [
    os.path.join(ROOT, "dashboard_server.py"),
    os.path.join(ROOT, "dev_core", "alerts.py"),
    os.path.join(ROOT, "dev_core", "broker_sim.py"),
    os.path.join(ROOT, "dev_core", "broker_alpaca_rest.py"),
    os.path.join(ROOT, "dev_core", "storage.py"),
    os.path.join(ROOT, "dev_core", "edge_filter.py"),
    os.path.join(ROOT, "portfolio_backtest.py"),
    os.path.join(ROOT, "portfolio_rebalance.py"),
    os.path.join(ROOT, "compute_exec_labels.py"),
    os.path.join(ROOT, "train_size_policy.py"),
]

SMOKE_CMDS = [
    ("compute_exec_labels.py", [sys.executable, "-u", os.path.join(ROOT, "compute_exec_labels.py")]),
    ("train_size_policy.py", [sys.executable, "-u", os.path.join(ROOT, "train_size_policy.py")]),
    ("portfolio_backtest.py", [sys.executable, "-u", os.path.join(ROOT, "portfolio_backtest.py")]),
    ("portfolio_rebalance.py", [sys.executable, "-u", os.path.join(ROOT, "portfolio_rebalance.py")]),
    ("dev_core/broker_sim.py", [sys.executable, "-u", os.path.join(ROOT, "dev_core", "broker_sim.py")]),
]


def _t_ms() -> int:
    return int(time.time() * 1000)


def _compile_files(files: List[str]) -> List[str]:
    errs: List[str] = []
    for f in files:
        try:
            py_compile.compile(f, doraise=True)
        except Exception as e:
            errs.append(f"{f}: {e}")
    return errs


def _ensure_schemas() -> List[str]:
    notes: List[str] = []
    from engine.dev_core.storage import init_db, connect
    from engine.dev_core.alerts import init_alerts_db
    from engine.dev_core.execution_ledger import init_execution_ledger
    import portfolio_backtest as pbt

    init_db()
    notes.append("core db ok")

    init_alerts_db()
    notes.append("alerts schema ok")

    init_execution_ledger()
    notes.append("execution ledger schema ok")

    con = connect()
    try:
        con.executescript(pbt.SCHEMA)
        con.commit()
    finally:
        con.close()
    notes.append("portfolio backtest schema ok")

    return notes


def _table_exists(con, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (str(name),),
    ).fetchone()
    return bool(row)


def _verify_required_tables() -> Tuple[List[str], List[str]]:
    req = [
        "events", "prices",
        "alerts",
        "execution_orders", "execution_fills", "execution_metrics", "pnl_attribution",
        "labels_exec",
    ]
    from engine.dev_core.storage import connect
    con = connect()
    missing: List[str] = []
    try:
        for t in req:
            if not _table_exists(con, t):
                missing.append(t)
    finally:
        con.close()

    notes: List[str] = []
    if not missing:
        notes.append("required tables ok")
    return notes, missing


def _run_cmd(name: str, argv: List[str], timeout_s: int) -> Tuple[int, str]:
    try:
        p = subprocess.run(
            argv,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            check=False,
            text=True,
        )
        out = (p.stdout or "").strip()
        return int(p.returncode), out
    except subprocess.TimeoutExpired:
        return 124, f"{name}: timeout after {timeout_s}s"
    except Exception as e:
        return 125, f"{name}: {e}"


def _exec_cost_gate_sanity() -> Tuple[List[str], List[str]]:
    warnings: List[str] = []
    notes: List[str] = []

    if os.environ.get("ALERT_USE_EXEC_COST_FILTER", "0") != "1":
        return notes, warnings

    try:
        from engine.dev_core.storage import connect
        from engine.dev_core.edge_filter import adjust_expected_z_for_costs

        con = connect()
        try:
            row = con.execute(
                "SELECT symbol FROM prices ORDER BY ts_ms DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()

        if not row or not row[0]:
            warnings.append("exec_cost_filter enabled but prices table empty")
            return notes, warnings

        sym = str(row[0])
        adj = adjust_expected_z_for_costs(
            symbol=sym,
            horizon_s=300,
            expected_z=1.0,
            side=1,
        )
        if adj is None:
            warnings.append(f"exec_cost_filter enabled but no realized vol for symbol={sym}")
        else:
            notes.append(f"exec_cost_filter ok symbol={sym}")
    except Exception as e:
        warnings.append(f"exec_cost_filter sanity failed: {e}")

    return notes, warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout_s", type=int, default=int(os.environ.get("PREFLIGHT_SMOKE_TIMEOUT_S", "900")))
    args = ap.parse_args()

    started = _t_ms()
    result: Dict[str, object] = {
        "ok": False,
        "started_ts_ms": started,
        "steps": [],
        "warnings": [],
        "errors": [],
        "smoke": [],
    }

    comp_errs = _compile_files(KEY_FILES)
    if comp_errs:
        result["errors"] = list(result["errors"]) + comp_errs
        if args.json:
            print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        else:
            for e in comp_errs:
                print("[compile]", e)
        return 3
    result["steps"].append("py_compile ok")

    try:
        result["steps"].extend(_ensure_schemas())
    except Exception as e:
        result["errors"].append(f"schema init failed: {e}")
        if args.json:
            print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        else:
            print("[schema]", e)
        return 3

    notes, missing = _verify_required_tables()
    result["steps"].extend(notes)
    if missing:
        result["errors"].append("missing tables: " + ",".join(missing))
        if args.json:
            print(json.dumps(result, separators=(",", ":"), sort_keys=True))
        return 3

    for name, argv in SMOKE_CMDS:
        rc, out = _run_cmd(name, argv, timeout_s=int(args.timeout_s))
        result["smoke"].append({"name": name, "rc": rc, "out": out[-4000:]})
        if rc != 0:
            result["errors"].append(f"smoke failed: {name} rc={rc}")
            if args.json:
                print(json.dumps(result, separators=(",", ":"), sort_keys=True))
            else:
                print(f"[smoke] {name} rc={rc}\n{out}")
            return 3

    notes2, warns2 = _exec_cost_gate_sanity()
    result["steps"].extend(notes2)
    result["warnings"].extend(warns2)

    result["ok"] = True
    result["finished_ts_ms"] = _t_ms()
    result["duration_ms"] = int(result["finished_ts_ms"]) - int(result["started_ts_ms"])

    if args.json:
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))

    return 2 if result["warnings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
