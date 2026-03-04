from typing import Dict, Any, List


HYPOTHESIS_SYSTEM_PROMPT = (
    "You are an offline Autonomous Research AI for a trading system. "
    "You must only generate research hypotheses and offline test plans. "
    "You must never propose deployment steps, live trading actions, or promotion. "
    "All outputs must be safe for human review."
)


def prompt_template() -> str:
    return (
        HYPOTHESIS_SYSTEM_PROMPT
        + "\n\n"
        + "Return JSON with keys: title, hypothesis, rationale, offline_evaluation_plan, risk_and_failure_modes, "
        + "config_overrides (env-style key/value pairs), and safety (explicitly state no live/promotion)."
    )


def seed_hypotheses() -> List[Dict[str, Any]]:
    """Deterministic seed hypotheses (no external model required).

    Each proposal uses env-style overrides so evaluation can be run without code changes.
    """

    return [
        {
            "title": "Increase min confidence threshold",
            "hypothesis": "Raising PORTFOLIO_MIN_CONF reduces false positives and improves risk-adjusted returns.",
            "rationale": [
                "Low-confidence alerts add noise and turnover.",
                "Higher threshold should reduce drawdowns at cost of fewer trades.",
            ],
            "config_overrides": {
                "PORTFOLIO_MIN_CONF": "0.70",
            },
            "offline_evaluation_plan": {
                "harness": "portfolio_backtest",
                "metrics": ["total_return", "max_drawdown", "sharpe_simple", "turnover_proxy"],
                "compare_to": "baseline env",
            },
            "risk_and_failure_modes": [
                "May undertrade and miss large moves.",
                "May concentrate exposures if fewer symbols qualify.",
            ],
            "safety": {"live_trading_changes": "none", "promotion": "human_approval_required"},
        },
        {
            "title": "Increase absolute z filter",
            "hypothesis": "Increasing PORTFOLIO_MIN_ABS_Z filters weak signals and improves net edge per trade.",
            "rationale": [
                "Small magnitude z signals are more sensitive to noise and costs.",
                "Filtering should reduce churn and improve Sharpe.",
            ],
            "config_overrides": {
                "PORTFOLIO_MIN_ABS_Z": "1.50",
            },
            "offline_evaluation_plan": {
                "harness": "portfolio_backtest",
                "metrics": ["total_return", "max_drawdown", "sharpe_simple"],
                "compare_to": "baseline env",
            },
            "risk_and_failure_modes": [
                "May reduce diversification.",
                "Potential regime sensitivity (trend vs shock).",
            ],
            "safety": {"live_trading_changes": "none", "promotion": "human_approval_required"},
        },
        {
            "title": "Reduce max positions to concentrate",
            "hypothesis": "Lowering PORTFOLIO_MAX_POSITIONS increases concentration in best signals, improving return at acceptable risk.",
            "rationale": [
                "If signal quality is heavy-tailed, concentrating can improve outcomes.",
                "Also reduces turnover and execution complexity.",
            ],
            "config_overrides": {
                "PORTFOLIO_MAX_POSITIONS": "6",
            },
            "offline_evaluation_plan": {
                "harness": "portfolio_backtest",
                "metrics": ["total_return", "max_drawdown", "sharpe_simple"],
                "compare_to": "baseline env",
            },
            "risk_and_failure_modes": [
                "Higher idiosyncratic risk.",
                "May worsen drawdowns in correlated selloffs.",
            ],
            "safety": {"live_trading_changes": "none", "promotion": "human_approval_required"},
        },
        {
            "title": "Tighten gross exposure cap",
            "hypothesis": "Reducing PORTFOLIO_GROSS_CAP lowers tail risk and improves drawdown control.",
            "rationale": [
                "Lower gross should reduce drawdown magnitude when many signals align.",
                "May sacrifice raw return but improve Calmar-like outcomes.",
            ],
            "config_overrides": {
                "PORTFOLIO_GROSS_CAP": "0.55",
            },
            "offline_evaluation_plan": {
                "harness": "portfolio_backtest",
                "metrics": ["total_return", "max_drawdown", "calmar_simple"],
                "compare_to": "baseline env",
            },
            "risk_and_failure_modes": [
                "May underperform in strongly trending periods.",
            ],
            "safety": {"live_trading_changes": "none", "promotion": "human_approval_required"},
        },
    ]
