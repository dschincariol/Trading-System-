# CREATE NEW FILE: api_ops.py
# Route specs for ops/diagnostics endpoints.
# This file contains only route metadata (no runtime imports from dashboard_server.py).

ROUTE_SPECS = [
    ("GET", "/api/alerts", "api_get_alerts"),
    ("GET", "/api/validation", "api_get_validation"),
    ("GET", "/api/model/diagnostics", "api_get_model_diagnostics"),
    ("GET", "/api/model/registry", "get_model_registry"),
    ("GET", "/api/embed_model_eval", "get_embed_model_eval"),
    ("GET", "/api/embed_conf_calib", "get_embed_conf_calib"),
    ("GET", "/api/temporal/eval", "get_temporal_eval"),
    ("GET", "/api/temporal/models", "get_temporal_models"),
    ("GET", "/api/portfolio/backtest/latest", "get_latest_portfolio_backtest"),
    ("GET", "/api/execution/metrics", "api_get_execution_metrics"),
    ("GET", "/api/execution/metrics/rolling", "api_get_execution_metrics_rolling"),
    ("GET", "/api/execution/metrics/by_symbol", "get_execution_metrics_by_symbol"),
    ("GET", "/api/execution/metrics/by_confidence", "get_execution_cost_by_confidence"),
    ("GET", "/api/social/features", "get_social_features"),
    ("GET", "/api/social/regimes", "get_social_regimes"),
    ("GET", "/api/social/blocks", "get_social_blocks"),
    ("GET", "/api/confidence_mass", "api_get_confidence_mass"),
    ("POST", "/api/promotion/rollback", "api_post_rollback"),
]
