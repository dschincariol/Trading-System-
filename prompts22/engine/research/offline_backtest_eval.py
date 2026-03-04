import os
import json
from dataclasses import dataclass
from typing import Dict, Any, Optional


@dataclass(frozen=True)
class BacktestResult:
    ok: bool
    run_id: Optional[int]
    metrics: Dict[str, Any]
    error: Optional[str] = None


def _env_snapshot(keys) -> Dict[str, str]:
    out = {}
    for k in keys:
        if k in os.environ:
            out[str(k)] = str(os.environ.get(k))
    return out


def run_portfolio_backtest(*, env_overrides: Optional[Dict[str, str]] = None) -> BacktestResult:
    """Run the existing portfolio backtest harness with optional env overrides."""

    prior = {}
    if env_overrides:
        for k, v in env_overrides.items():
            k = str(k)
            prior[k] = os.environ.get(k)
            os.environ[k] = str(v)

    try:
        import engine.strategy.portfolio_backtest as portfolio_backtest

        res = portfolio_backtest.run_backtest() or {}
        ok = bool(res.get("ok"))
        run_id = res.get("run_id")
        try:
            run_id = int(run_id) if run_id is not None else None
        except Exception:
            run_id = None
        metrics = res.get("metrics") or {}
        if not isinstance(metrics, dict):
            metrics = {}

        # Add traceability
        metrics = dict(metrics)
        metrics["_research_env"] = _env_snapshot(
            [
                "BT_DAYS",
                "BT_LOOKBACK_S",
                "BT_START_EQUITY",
                "PORTFOLIO_MIN_CONF",
                "PORTFOLIO_MIN_ABS_Z",
                "PORTFOLIO_MAX_POSITIONS",
                "PORTFOLIO_GROSS_CAP",
                "PORTFOLIO_MAX_W_PER_SYMBOL",
            ]
        )

        return BacktestResult(ok=ok, run_id=run_id, metrics=metrics)
    except Exception as e:
        return BacktestResult(ok=False, run_id=None, metrics={}, error=repr(e))
    finally:
        if env_overrides:
            for k, old in prior.items():
                if old is None:
                    try:
                        del os.environ[k]
                    except Exception:
                        pass
                else:
                    os.environ[k] = str(old)


def score_metrics(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Compute a single composite score for ranking (higher is better)."""

    def f(x, d=0.0):
        try:
            return float(x)
        except Exception:
            return float(d)

    total_return = f(metrics.get("total_return"), 0.0)
    sharpe = f(metrics.get("sharpe_simple"), 0.0)
    calmar = f(metrics.get("calmar_simple"), 0.0)

    # max_drawdown in portfolio_backtest is negative (min drawdown), so abs it
    max_dd = abs(f(metrics.get("max_drawdown"), 0.0))

    # Conservative composite: prioritize positive return, penalize drawdown
    score = (
        1.0 * total_return
        + 0.20 * sharpe
        + 0.15 * calmar
        - 0.75 * max_dd
    )

    return {
        "score": float(score),
        "components": {
            "total_return": float(total_return),
            "sharpe_simple": float(sharpe),
            "calmar_simple": float(calmar),
            "max_drawdown_abs": float(max_dd),
        },
        "weights": {
            "total_return": 1.0,
            "sharpe_simple": 0.20,
            "calmar_simple": 0.15,
            "max_drawdown_abs": -0.75,
        },
    }


def format_result_json(result: BacktestResult) -> str:
    payload = {
        "ok": bool(result.ok),
        "run_id": result.run_id,
        "metrics": result.metrics,
        "error": result.error,
    }
    return json.dumps(payload, indent=2, sort_keys=True)
