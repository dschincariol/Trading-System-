import json
import os
import sys
from typing import Dict, Any, List

from engine.research.sandbox import sandbox_bootstrap, assert_no_live_imports
from engine.research.hypothesis_prompts import seed_hypotheses
from engine.research.offline_backtest_eval import run_portfolio_backtest
from engine.research.rank_proposals import rank


def _print(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    sys.stdout.flush()


def _proposal_to_env_overrides(p: Dict[str, Any]) -> Dict[str, str]:
    overrides = p.get("config_overrides") or {}
    if not isinstance(overrides, dict):
        overrides = {}
    return {str(k): str(v) for k, v in overrides.items()}


def run() -> Dict[str, Any]:
    # Sandbox first (before importing backtest harness)
    bootstrap = sandbox_bootstrap(
        source_db_path=os.environ.get("RESEARCH_DB_SOURCE"),
        target_db_path=os.environ.get("RESEARCH_DB_PATH"),
        copy_db=os.environ.get("RESEARCH_DB_COPY", "1") == "1",
    )

    # Fail-closed if anything suspicious got imported
    assert_no_live_imports()

    proposals: List[Dict[str, Any]] = seed_hypotheses()

    # Run backtests (offline only)
    evaluated: List[Dict[str, Any]] = []
    for p in proposals:
        env = _proposal_to_env_overrides(p)
        bt = run_portfolio_backtest(env_overrides=env)
        out = dict(p)
        out["backtest_ok"] = bool(bt.ok)
        out["backtest_run_id"] = bt.run_id
        out["backtest_metrics"] = bt.metrics
        out["backtest_error"] = bt.error
        evaluated.append(out)

    ranked = rank(evaluated)

    return {
        "ok": True,
        "sandbox": bootstrap,
        "n_proposals": len(proposals),
        "ranked": ranked,
        "safety": {
            "offline_only": True,
            "no_promotion": True,
            "no_live_trading": True,
            "human_review_required": True,
        },
    }


def main() -> None:
    res = run()
    _print(res)


if __name__ == "__main__":
    main()
