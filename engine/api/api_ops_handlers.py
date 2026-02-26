# engine/api/api_ops_handlers.py
"""
Ops / diagnostics endpoints.

Contains handler implementations only.
No dashboard_server imports.
All runtime objects are accessed via ctx.
"""

from __future__ import annotations
from engine.runtime.gates import execution_gate_snapshot
from engine.api.http_parsing import qs as _qs

# ----------------------------
# Simple pass-through GETs
# ----------------------------

def api_get_alerts(_parsed, ctx):
    from engine.api.api_read import get_alerts
    return get_alerts()


def api_get_validation(_parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_validation
    return api_get_validation(_parsed, ctx)


def api_get_model_diagnostics(_parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_model_diagnostics
    return api_get_model_diagnostics(_parsed, ctx)


def api_get_model_registry(parsed, ctx):
    from engine.api.api_read import get_model_registry
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_model_registry(limit=limit)


def api_get_embed_model_eval(parsed, ctx):
    from engine.api.api_read import get_embed_model_eval
    qs = _qs(parsed)
    limit = int(qs.get("limit", "500") or "500")
    return get_embed_model_eval(limit=limit)


def api_get_embed_conf_calib(parsed, ctx):
    from engine.api.api_read import get_embed_conf_calib
    qs = _qs(parsed)
    horizon_s = int(qs.get("horizon_s", "0") or "0")
    model_kind = str(qs.get("model_kind", "") or "")
    limit = int(qs.get("limit", "200") or "200")
    return get_embed_conf_calib(
        horizon_s=horizon_s,
        model_kind=model_kind,
        limit=limit,
    )


def api_get_temporal_eval(parsed, ctx):
    from engine.api.api_read import get_temporal_eval
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_temporal_eval(limit=limit)


def api_get_model_diagnostics(_parsed, ctx):
    from engine.api.api_read_advanced import get_model_diagnostics
    return {"ok": True, "data": get_model_diagnostics()}

def api_get_latest_portfolio_backtest(_parsed, ctx):
    from engine.api.api_read_advanced import get_latest_portfolio_backtest
    return get_latest_portfolio_backtest()

def api_get_execution_metrics(_parsed, ctx):
    from engine.api.api_read import get_execution_metrics
    return get_execution_metrics()


def api_get_execution_metrics_rolling(_parsed, ctx):
    from engine.api.api_read_advanced import get_execution_metrics_rolling
    return get_execution_metrics_rolling()


def api_get_execution_metrics_by_symbol(parsed, ctx):
    from engine.api.api_read_advanced import get_execution_metrics_by_symbol
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_execution_metrics_by_symbol(limit=limit)

def api_get_execution_cost_by_confidence(_parsed, ctx):
    from engine.api.api_read_advanced import get_execution_cost_by_confidence
    return get_execution_cost_by_confidence()

def api_get_social_features(parsed, ctx):
    from engine.api.api_read_advanced import get_social_features
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    return get_social_features(symbol=symbol, limit=limit)

def api_get_social_regimes(parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_social_regimes
    return api_get_social_regimes(parsed, ctx)


def api_get_social_blocks(parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_social_blocks
    return api_get_social_blocks(parsed, ctx)


def api_get_confidence_mass(_parsed, ctx):
    from engine.api.api_read import get_confidence_mass
    return get_confidence_mass()


# ----------------------------
# POST
# ----------------------------

def api_post_rollback(parsed, body, ctx):
    # Fail-closed if shutting down
    denied = _deny_if_shutdown()
    if denied:
        return denied

    # Hard execution gate (rollback mutates champion model)
    gate = execution_gate_snapshot(
        get_execution_mode_fn=lambda: ctx.get("JOBS").get_execution_mode_fn()
        if ctx.get("JOBS") else None
    )
    if not gate.get("allow_execution"):
        return {
            "ok": False,
            "error": f"execution_gated:{gate.get('reason')}",
            "gate": gate,
        }

    from engine.api.api_governance import api_post_rollback
    return api_post_rollback(parsed, body)
