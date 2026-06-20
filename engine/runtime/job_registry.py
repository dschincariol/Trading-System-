# engine/runtime/job_registry.py
"""
Single source of truth for:
- ALLOWED_JOBS
- PIPELINE_ORDER
- JOB_ORDER

Production-safe ordering.
Ensures at least one price daemon and ingest pipeline start.
"""

ALLOWED_JOBS = {

    # ---------------------------
    # PRICE FEEDS (daemon group)
    # ---------------------------

    "stream_prices_polygon_ws": (
        "engine/jobs/stream_prices_polygon_ws.py",
        "daemon",
        "price_feed",
        {"execution": False},
    ),

    "stream_prices_ibkr": (
        "engine/data/stream_prices_ibkr.py",
        "daemon",
        "price_feed",
        {"execution": False},
    ),

    "poll_prices": (
        "engine/data/poll_prices.py",
        "daemon",
        "price_feed",
        {"execution": False},
    ),

    # ---------------------------
    # Provider monitor
    # ---------------------------

    "provider_monitor": (
        "engine/runtime/jobs/provider_monitor_job.py",
        "daemon",
        None,
        {"execution": False},
    ),

    # ---------------------------
    # Core data pipeline
    # ---------------------------

    "ingest_now": (
        "engine/data/jobs/ingest_now.py",
        "oneshot",
        None,
        {"execution": False},
    ),

    "process_events": (
        "engine/data/jobs/process_events.py",
        "oneshot",
        None,
        {"execution": False},
    ),

    "label_due_events": (
        "engine/data/jobs/label_due_events.py",
        "oneshot",
        None,
        {"execution": False},
    ),

    "compute_drift": (
        "engine/data/jobs/compute_drift.py",
        "oneshot",
        None,
        {"execution": False},
    ),

    # ---------------------------
    # Model
    # ---------------------------

    "train_embed_models": (
        "engine/strategy/jobs/train_embed_models.py",
        "oneshot",
        None,
        {"execution": False},
    ),

    "train_model_v2": (
        "engine/strategy/jobs/train_model_v2.py",
        "oneshot",
        None,
        {"execution": False},
    ),

    "validate_now": (
        "engine/strategy/jobs/validate_now.py",
        "oneshot",
        None,
        {"execution": False},
    ),

    # ---------------------------
    # Execution
    # ---------------------------

    "portfolio_rebalance": (
        "engine/execution/jobs/portfolio_rebalance.py",
        "oneshot",
        None,
        {"execution": False},
    ),

    "broker_apply_orders": (
        "engine/execution/jobs/broker_apply_orders.py",
        "oneshot",
        None,
        {"execution": True},
    ),
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
    "portfolio_rebalance",
    "broker_apply_orders",
]


JOB_ORDER = [
    # Start price feed FIRST
    "stream_prices_polygon_ws",
    "stream_prices_ibkr",
    "poll_prices",

    # Monitor
    "provider_monitor",

    # Data pipeline
    "ingest_now",
    "process_events",
    "label_due_events",
    "compute_drift",

    # Model
    "train_embed_models",
    "train_model_v2",
    "validate_now",

    # Execution
    "portfolio_rebalance",
    "broker_apply_orders",
]