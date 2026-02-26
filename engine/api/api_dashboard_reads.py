"""
Dashboard Read Endpoints (moved out of dashboard_server)

Goal:
- dashboard_server.py is orchestration only
- ALL DB reads and dev_core reads live behind engine/api
"""

import json

from engine.api.http_parsing import qs as _qs

from engine.api.api_read_advanced import (
    get_model_diagnostics,
    get_temporal_models,
    get_latest_portfolio_backtest,
    get_execution_metrics_by_symbol,
    get_execution_cost_by_confidence,
    get_social_features,
    get_social_regimes,
    get_social_blocks,
    get_validation_rows,
    get_shadow_capital_scores,
    run_shadow_capital_scores,
    get_size_policy,
)

# ------------------------------
# Handlers (HTTP signatures)
# build_handler calls:
#   GET: handler(parsed, ctx)
#   POST: handler(parsed, body, ctx)
# ------------------------------

def api_get_model_diagnostics(_parsed, _ctx=None):
    return {"ok": True, "data": get_model_diagnostics()}

def api_get_temporal_models(parsed, ctx):
    from engine.api.api_read_advanced import get_temporal_models
    qs = _qs(parsed)
    limit = int(qs.get("limit", "20") or "20")
    return get_temporal_models(limit=limit)

def api_get_latest_portfolio_backtest(_parsed, _ctx=None):
    return get_latest_portfolio_backtest()

def api_get_execution_metrics_by_symbol(parsed, _ctx=None):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_execution_metrics_by_symbol(limit=limit)

def api_get_execution_cost_by_confidence(_parsed, _ctx=None):
    return get_execution_cost_by_confidence()

def api_get_social_features(parsed, _ctx=None):
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    return get_social_features(symbol=symbol, limit=limit)

def api_get_social_regimes(parsed, _ctx=None):
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    return get_social_regimes(symbol=symbol, limit=limit)

def api_get_social_blocks(parsed, _ctx=None):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "200") or "200")
    return get_social_blocks(limit=limit)

def api_get_validation(_parsed, _ctx=None):
    return get_validation_rows()

def api_get_size_policy(_parsed, _ctx=None):
    return get_size_policy()

def api_get_shadow_capital_scores(parsed, _ctx=None):
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    regime = str(qs.get("regime", "global") or "global").strip() or "global"
    return get_shadow_capital_scores(limit=limit, regime=regime)


def api_post_shadow_capital_run(parsed, body, _ctx=None):
    qs = _qs(parsed)
    window_s = qs.get("window_s", "")
    regime = qs.get("regime", "")

    if isinstance(body, dict):
        if not window_s:
            window_s = body.get("window_s", "")
        if not regime:
            regime = body.get("regime", "")

    try:
        window_s = int(window_s or 86400)
    except Exception:
        window_s = 86400

    regime = str(regime or "global").strip() or "global"

    return run_shadow_capital_scores(window_s=window_s, regime=regime)
