import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Replace exact module prefixes anywhere in .py files.
# Longest-first to avoid partial overlaps.
MAP = {
    "engine.dev_core.storage": "engine.runtime.storage",
    "engine.dev_core.alerts": "engine.runtime.alerts",
    "engine.dev_core.health": "engine.runtime.health",
    "engine.dev_core.dashboard_weather_widgets": "engine.runtime.dashboard_weather_widgets",

    "engine.dev_core.asset_map": "engine.data.asset_map",
    "engine.dev_core.equity_snapshot": "engine.data.equity_snapshot",
    "engine.dev_core.factor_universe": "engine.data.factor_universe",
    "engine.dev_core.gdelt_macro": "engine.data.gdelt_macro",
    "engine.dev_core.provider_router": "engine.data.provider_router",
    "engine.dev_core.universe": "engine.data.universe",
    "engine.dev_core.universe_discovery": "engine.data.universe_discovery",
    "engine.dev_core.weather_features": "engine.data.weather_features",
    "engine.dev_core.weather_api": "engine.data.weather_api",
    "engine.dev_core.symbol_blacklist": "engine.data.symbol_blacklist",

    "engine.dev_core.calendar.fmp_earnings": "engine.data.calendar.fmp_earnings",
    "engine.dev_core.ingest.gdelt_ingest": "engine.data.ingest.gdelt_ingest",
    "engine.dev_core.ingest.options_polygon": "engine.data.options.options_polygon",

    "engine.dev_core.live_prices.ccxt_live": "engine.data.live_prices.ccxt_live",
    "engine.dev_core.live_prices.ibkr_live": "engine.data.live_prices.ibkr_live",
    "engine.dev_core.live_prices.polygon_live": "engine.data.live_prices.polygon_live",
    "engine.dev_core.live_prices.provider": "engine.data.live_prices.provider",
    "engine.dev_core.live_prices.yfinance_live": "engine.data.live_prices.yfinance_live",

    "engine.dev_core.options.options_polygon": "engine.data.options.options_polygon",
    "engine.dev_core.options.tradier_live": "engine.data.options.tradier_live",

    "engine.dev_core.prices.csv_feed": "engine.data.prices.csv_feed",
    "engine.dev_core.prices.returns": "engine.data.prices.returns",
    "engine.dev_core.prices.volatility": "engine.data.prices.volatility",

    "engine.dev_core.sec.edgar_live": "engine.data.sec.edgar_live",

    "engine.dev_core.broker_alpaca_rest": "engine.execution.broker_alpaca_rest",
    "engine.dev_core.broker_fill_utils": "engine.execution.broker_fill_utils",
    "engine.dev_core.broker_ibkr_gateway": "engine.execution.broker_ibkr_gateway",
    "engine.dev_core.broker_router": "engine.execution.broker_router",
    "engine.dev_core.broker_sim": "engine.execution.broker_sim",
    "engine.dev_core.dual_execution": "engine.execution.dual_execution",
    "engine.dev_core.exec_conf_calibration": "engine.execution.exec_conf_calibration",
    "engine.dev_core.exec_stats": "engine.execution.exec_stats",
    "engine.dev_core.execution_analytics_engine": "engine.execution.execution_analytics_engine",
    "engine.dev_core.execution_costs": "engine.execution.execution_costs",
    "engine.dev_core.execution_ledger": "engine.execution.execution_ledger",
    "engine.dev_core.execution_microstructure": "engine.execution.execution_microstructure",
    "engine.dev_core.execution_mode": "engine.execution.execution_mode",
    "engine.dev_core.execution_policy_engine": "engine.execution.execution_policy_engine",
    "engine.dev_core.kill_switch": "engine.execution.kill_switch",
    "engine.dev_core.kill_switch.snapshot": "engine.execution.kill_switch",
    "engine.dev_core.position_reconcile": "engine.execution.position_reconcile",
    "engine.dev_core.trade_attribution_ledger": "engine.execution.trade_attribution_ledger",

    "engine.dev_core.adaptive_order_slicer": "engine.strategy.adaptive_order_slicer",
    "engine.dev_core.alpha_lifecycle_engine": "engine.strategy.alpha_lifecycle_engine",
    "engine.dev_core.capital_guard": "engine.strategy.capital_guard",
    "engine.dev_core.clustering": "engine.strategy.clustering",
    "engine.dev_core.confidence_adjust": "engine.strategy.confidence_adjust",
    "engine.dev_core.corr_opt": "engine.strategy.corr_opt",
    "engine.dev_core.decision_log": "engine.strategy.decision_log",
    "engine.dev_core.drawdown_state": "engine.strategy.drawdown_state",
    "engine.dev_core.drift": "engine.strategy.drift",
    "engine.dev_core.drift_utils": "engine.strategy.drift_utils",
    "engine.dev_core.edge_filter": "engine.strategy.edge_filter",
    "engine.dev_core.embed_regressor": "engine.strategy.embed_regressor",
    "engine.dev_core.feature_expansion": "engine.strategy.feature_expansion",
    "engine.dev_core.labeling": "engine.strategy.labeling",
    "engine.dev_core.learning": "engine.strategy.learning",
    "engine.dev_core.market_stress": "engine.strategy.market_stress",
    "engine.dev_core.model_registry": "engine.strategy.model_registry",
    "engine.dev_core.model_v2": "engine.strategy.model_v2",
    "engine.dev_core.news_domain": "engine.strategy.news_domain",
    "engine.dev_core.opportunity_allocation": "engine.strategy.opportunity_allocation",
    "engine.dev_core.pnl_decomposition_engine": "engine.strategy.pnl_decomposition_engine",
    "engine.dev_core.portfolio": "engine.strategy.portfolio",
    "engine.dev_core.portfolio_execution_intents": "engine.strategy.portfolio_execution_intents",
    "engine.dev_core.portfolio_risk_gate": "engine.strategy.portfolio_risk_gate",
    "engine.dev_core.predictor": "engine.strategy.predictor",
    "engine.dev_core.promotion_audit": "engine.strategy.promotion_audit",
    "engine.dev_core.promotion_guard": "engine.strategy.promotion_guard",
    "engine.dev_core.promotion_hardening": "engine.strategy.promotion_hardening",
    "engine.dev_core.regime_compat": "engine.strategy.regime_compat",
    "engine.dev_core.regime_size": "engine.strategy.regime_size",
    "engine.dev_core.regime_stack": "engine.strategy.regime_stack",
    "engine.dev_core.risk": "engine.strategy.risk",
    "engine.dev_core.risk_state": "engine.strategy.risk_state",
    "engine.dev_core.rl_strategy_policy": "engine.strategy.rl_strategy_policy",
    "engine.dev_core.rules_engine": "engine.strategy.rules_engine",
    "engine.dev_core.shadow_trainer": "engine.strategy.shadow_trainer",
    "engine.dev_core.size_policy": "engine.strategy.size_policy",
    "engine.dev_core.social_context": "engine.strategy.social_context",
    "engine.dev_core.social_regime": "engine.strategy.social_regime",
    "engine.dev_core.social_risk": "engine.strategy.social_risk",
    "engine.dev_core.strategy_selector": "engine.strategy.strategy_selector",
    "engine.dev_core.tech_indicators": "engine.strategy.tech_indicators",
    "engine.dev_core.temporal_encoder": "engine.strategy.temporal_encoder",
    "engine.dev_core.temporal_predictor": "engine.strategy.temporal_predictor",
    "engine.dev_core.training_guard": "engine.strategy.training_guard",
    "engine.dev_core.validation": "engine.strategy.validation",

    # dev_core.regime was referenced but no module exists; direct to model_v2 where get_current_regime lives
    "engine.dev_core.regime": "engine.strategy.model_v2",
}

ORDER = sorted(MAP.keys(), key=len, reverse=True)

def patch_file(path: Path) -> bool:
    src = path.read_text(encoding="utf-8", errors="ignore")
    out = src
    for k in ORDER:
        out = out.replace(k, MAP[k])
    if out != src:
        path.write_text(out, encoding="utf-8")
        return True
    return False

def main() -> int:
    changed = []
    for p in REPO_ROOT.rglob("*.py"):
        # do not rewrite inside venvs or caches if present
        if any(part in (".venv", "venv", "__pycache__", ".git") for part in p.parts):
            continue
        if patch_file(p):
            changed.append(str(p.relative_to(REPO_ROOT)))
    if changed:
        print("CHANGED:")
        for x in changed:
            print("  " + x)
    else:
        print("NO CHANGES")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
