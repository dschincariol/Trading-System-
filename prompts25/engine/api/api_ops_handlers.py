# engine/api/api_ops_handlers.py
"""
Ops / diagnostics endpoints.

Contains handler implementations only.
No dashboard_server imports.
All runtime objects are accessed via ctx.
"""

from __future__ import annotations

from urllib.parse import parse_qs

from engine.runtime.lifecycle import snapshot as lifecycle_snapshot
from engine.runtime.gates import execution_gate_snapshot

def _qs(parsed):
    try:
        q = parse_qs(parsed.query or "")
        return {k: v[0] for k, v in q.items()}
    except Exception:
        return {}


def _deny_if_shutdown():
    try:
        snap = lifecycle_snapshot() or {}
        if str(snap.get("state") or "").upper() == "SHUTDOWN":
            return {"ok": False, "error": "server_shutting_down"}
    except Exception:
        pass
    return None


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


def api_get_temporal_models(parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_temporal_models
    return api_get_temporal_models(parsed, ctx)


def api_get_latest_portfolio_backtest(_parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_latest_portfolio_backtest
    return api_get_latest_portfolio_backtest(_parsed, ctx)


def api_get_execution_metrics(_parsed, ctx):
    from engine.api.api_read import get_execution_metrics
    return get_execution_metrics()


def api_get_execution_metrics_rolling(_parsed, ctx):
    from engine.api.api_read_advanced import get_execution_metrics_rolling
    return get_execution_metrics_rolling()


def api_get_execution_metrics_by_symbol(parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_execution_metrics_by_symbol
    return api_get_execution_metrics_by_symbol(parsed, ctx)


def api_get_execution_cost_by_confidence(parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_execution_cost_by_confidence
    return api_get_execution_cost_by_confidence(parsed, ctx)


def api_get_social_features(parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_social_features
    return api_get_social_features(parsed, ctx)


def api_get_social_regimes(parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_social_regimes
    return api_get_social_regimes(parsed, ctx)


def api_get_social_blocks(parsed, ctx):
    from engine.api.api_dashboard_reads import api_get_social_blocks
    return api_get_social_blocks(parsed, ctx)


def api_get_confidence_mass(_parsed, ctx):
    from engine.api.api_read import get_confidence_mass
    return get_confidence_mass()


def api_get_ai_ops_explain(_parsed, ctx):
    """Handler invoked by /api/ai/ops_explain.

    Delegates to the same logic implemented in api_ops.py; this indirection
    keeps dashboard_server imports lightweight.
    """
    try:
        from engine.api.api_ops import api_get_ai_ops_explain as _impl
        return _impl(_parsed, ctx)
    except Exception as e:
        return {"ok": False, "error": f"ai_ops_impl_error:{e}"}


def api_get_self_critic_warnings(parsed, ctx):
    from engine.strategy.self_critic import get_warnings
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    lookback_ms = int(qs.get("lookback_ms", str(24 * 60 * 60 * 1000)) or "0")
    min_severity = str(qs.get("min_severity", "INFO") or "INFO").upper().strip()
    return get_warnings(min_severity=min_severity, lookback_ms=lookback_ms, limit=limit)


def api_post_self_critic_run(parsed, body, ctx):
    denied = _deny_if_shutdown()
    if denied:
        return denied

    from engine.strategy.self_critic import upsert_warnings

    qs = _qs(parsed)
    lookback_ms = None
    max_rows = None

    try:
        if qs.get("lookback_ms") is not None:
            lookback_ms = int(qs.get("lookback_ms") or "0")
    except Exception:
        lookback_ms = None

    try:
        if qs.get("max_rows") is not None:
            max_rows = int(qs.get("max_rows") or "0")
    except Exception:
        max_rows = None

    if isinstance(body, dict):
        if lookback_ms is None and body.get("lookback_ms") is not None:
            try:
                lookback_ms = int(body.get("lookback_ms"))
            except Exception:
                lookback_ms = None
        if max_rows is None and body.get("max_rows") is not None:
            try:
                max_rows = int(body.get("max_rows"))
            except Exception:
                max_rows = None

    if lookback_ms is None:
        lookback_ms = 7 * 24 * 60 * 60 * 1000
    if max_rows is None:
        max_rows = 5000

    return upsert_warnings(lookback_ms=lookback_ms, max_rows=max_rows)


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
