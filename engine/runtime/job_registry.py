# engine/runtime/job_registry.py
"""
Single source of truth for:
- ALLOWED_JOBS
- PIPELINE_ORDER
- JOB_ORDER

Notes:
- Script paths are real paths that exist in this repo.
- Optional 3rd tuple element is `group` (used to enforce exclusive daemons per-group).
"""

ALLOWED_JOBS = {
    # Promotion / safety / monitors
    "post_promotion_monitor": ("post_promotion_monitor.py", "oneshot", None, {"execution": False}),
    "kill_slippage_monitor": ("kill_slippage_monitor.py", "oneshot", None, {"execution": False}),
    "kill_drift_monitor": ("kill_drift_monitor.py", "oneshot", None, {"execution": False}),
    "kill_health_monitor": ("kill_health_monitor.py", "oneshot", None, {"execution": False}),
    "snapshot_equity": ("snapshot_equity.py", "oneshot", None, {"execution": False}),
    "train_drawdown_policy": ("train_drawdown_policy.py", "oneshot", None, {"execution": False}),
    "train_size_policy": ("train_size_policy.py", "oneshot", None, {"execution": False}),
    "compute_exec_labels_from_fills": ("compute_exec_labels_from_fills.py", "oneshot", None, {"execution": False}),
    "compute_exec_labels": ("compute_exec_labels.py", "oneshot", None, {"execution": False}),
    "compute_exec_z": ("compute_exec_z.py", "oneshot", None, {"execution": False}),
    "recalibrate_confidence": ("recalibrate_confidence.py", "oneshot", None, {"execution": False}),

    # Prices (exclusive group: price_feed)
    "poll_prices": ("engine/data/poll_prices.py", "daemon", "price_feed", {"execution": False}),
    "stream_prices_polygon_ws": ("engine/data/stream_prices_polygon_ws.py", "daemon", "price_feed", {"execution": False}),
    "stream_prices_ibkr": ("engine/data/stream_prices_ibkr.py", "daemon", "price_feed", {"execution": False}),

    # Provider monitor (daemon)
    "provider_monitor": ("provider_monitor_job.py", "daemon", None, {"execution": False}),

    # Ingest / labeling / drift / calibration
    "ingest_now": ("ingest_now.py", "oneshot", None, {"execution": False}),
    "process_events": ("process_events.py", "oneshot", None, {"execution": False}),
    "label_due_events": ("label_due_events.py", "oneshot", None, {"execution": False}),
    "compute_drift": ("compute_drift.py", "oneshot", None, {"execution": False}),
    "calibrate_price_confidence": ("calibrate_price_confidence.py", "oneshot", None, {"execution": False}),
    "monitor_calibration_health": ("monitor_calibration_health.py", "oneshot", None, {"execution": False}),

    # A.1 supervised embed regressor training
    "train_embed_models": ("train_embed_models.py", "oneshot", None, {"execution": False}),
    "train_and_eval_challenger": ("pipeline_train_and_eval.py", "oneshot", None, {"execution": False}),

    # Backtests / scoring
    "backtest_walk_forward": ("backtest_walk_forward.py", "oneshot", None, {"execution": False}),
    "portfolio_backtest": ("portfolio_backtest.py", "oneshot", None, {"execution": False}),

    # Model
    "train_model_v2": ("train_model_v2.py", "oneshot", None, {"execution": False}),
    "validate_now": ("validate_now.py", "oneshot", None, {"execution": False}),

    # Checks
    "check_predictions": ("check_predictions.py", "oneshot", None, {"execution": False}),
    "check_events": ("check_events.py", "oneshot", None, {"execution": False}),
    "check_labels": ("check_labels.py", "oneshot", None, {"execution": False}),
    "check_alerts": ("check_alerts.py", "oneshot", None, {"execution": False}),

    # Portfolio + execution
    "portfolio_rebalance": ("portfolio_rebalance.py", "oneshot", None, {"execution": False}),
    "broker_apply_orders": ("broker_apply_orders.py", "oneshot", None, {"execution": True}),

    # Production preflight
    "prod_preflight": ("prod_preflight.py", "oneshot", None, {"execution": False}),
}

PIPELINE_ORDER = [
    "poll_prices",
    "ingest_now",
    "process_events",
    "label_due_events",
    "compute_drift",
    "train_embed_models",
    "train_model_v2",
    "validate_now",
    "process_events",
    "portfolio_rebalance",
    "broker_apply_orders",
]

JOB_ORDER = [
    "poll_prices",
    "stream_prices_polygon_ws",
    "stream_prices_ibkr",
    "provider_monitor",
    "ingest_now",
    "process_events",
    "label_due_events",
    "compute_drift",
    "post_promotion_monitor",
    "kill_health_monitor",
    "kill_drift_monitor",
    "kill_slippage_monitor",
    "train_embed_models",
    "train_model_v2",
    "validate_now",
    "check_predictions",
    "check_events",
    "check_labels",
    "check_alerts",
    "portfolio_rebalance",
    "portfolio_backtest",
    "broker_apply_orders",
    "backtest_walk_forward",
]
