import os
import sys
import time
import shutil
from pathlib import Path
from typing import Dict, Any, Optional


_DENY_IMPORT_PREFIXES = (
    "engine.execution",
    "engine.broker_",
    "engine.dual_execution",
    "engine.broker_router",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def apply_offline_guards(*, extra_env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Hard-set environment guards to prevent any live/promotion paths."""

    os.environ.setdefault("DISABLE_LIVE_EXECUTION", "1")
    os.environ.setdefault("EXECUTION_MODE_DEFAULT", "paper")
    os.environ.setdefault("EXECUTION_ARMED", "0")
    os.environ.setdefault("BROKER_NAME", "sim")

    os.environ.setdefault("PROMOTION_ENABLED", "0")
    os.environ.setdefault("PROMOTION_BLOCK_IF_EQUITY_CRIT", "1")

    if extra_env:
        for k, v in extra_env.items():
            os.environ[str(k)] = str(v)

    return {
        "ts_ms": _now_ms(),
        "guards": {
            "DISABLE_LIVE_EXECUTION": os.environ.get("DISABLE_LIVE_EXECUTION"),
            "EXECUTION_MODE_DEFAULT": os.environ.get("EXECUTION_MODE_DEFAULT"),
            "EXECUTION_ARMED": os.environ.get("EXECUTION_ARMED"),
            "BROKER_NAME": os.environ.get("BROKER_NAME"),
            "PROMOTION_ENABLED": os.environ.get("PROMOTION_ENABLED"),
        },
    }


def ensure_research_db(*, source_db_path: Optional[str] = None, target_db_path: Optional[str] = None, copy: bool = True) -> Dict[str, Any]:
    """Configure DB_PATH for offline research.

    If copy=True, copies source->target (fail-closed if source missing).
    If copy=False, uses target directly.

    Precedence:
      - source_db_path defaults to current DB_PATH env or 'dev.db'
      - target_db_path defaults to '.research/research.db'
    """

    src = Path(source_db_path or os.environ.get("DB_PATH", "dev.db")).expanduser().resolve()
    dst = Path(target_db_path or ".research/research.db").expanduser().resolve()

    dst.parent.mkdir(parents=True, exist_ok=True)

    if copy:
        if not src.exists():
            raise FileNotFoundError(f"research db source missing: {src}")
        shutil.copy2(src, dst)

    os.environ["DB_PATH"] = str(dst)

    return {
        "source": str(src),
        "target": str(dst),
        "copy": bool(copy),
    }


def assert_no_live_imports() -> None:
    """Fail-closed if any denied module appears in sys.modules."""

    bad = []
    for name in list(sys.modules.keys()):
        if not name:
            continue
        if name.startswith(_DENY_IMPORT_PREFIXES):
            bad.append(name)
        else:
            for p in _DENY_IMPORT_PREFIXES:
                if p.endswith("_") and name.startswith(p):
                    bad.append(name)
                    break

    if bad:
        bad_s = ",".join(sorted(set(bad))[:20])
        raise RuntimeError(f"research sandbox violation: live/execution modules imported: {bad_s}")


def sandbox_bootstrap(
    *,
    extra_env: Optional[Dict[str, str]] = None,
    source_db_path: Optional[str] = None,
    target_db_path: Optional[str] = None,
    copy_db: bool = True,
) -> Dict[str, Any]:
    """One-call bootstrap for offline research runs."""

    guard = apply_offline_guards(extra_env=extra_env)
    db = ensure_research_db(source_db_path=source_db_path, target_db_path=target_db_path, copy=copy_db)

    # Import-time check (before importing backtest modules)
    assert_no_live_imports()

    return {"guard": guard, "db": db}
