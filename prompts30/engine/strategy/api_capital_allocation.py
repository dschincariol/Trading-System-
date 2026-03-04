# API endpoints for capital allocation system

from engine.strategy.capital_allocation_job import (
    run_capital_allocation_job,
    get_capital_allocation_status,
    get_allocation_metrics
)


def api_get_capital_allocation_status(parsed):
    """Get current capital allocation status."""
    return get_capital_allocation_status()


def api_post_capital_allocation_run(parsed, body=None):
    """Trigger capital allocation run."""
    return run_capital_allocation_job()


def api_get_capital_allocation_metrics(parsed):
    """Get allocation performance metrics."""
    days = int(parsed.query.get('days', 30))
    return get_allocation_metrics(days)


# Route specifications
ROUTE_SPECS_CAPITAL_ALLOCATION = [
    {
        "method": "GET",
        "path": "/api/capital-allocation/status",
        "handler": "api_get_capital_allocation_status",
        "auth_required": False
    },
    {
        "method": "POST", 
        "path": "/api/capital-allocation/run",
        "handler": "api_post_capital_allocation_run",
        "auth_required": True
    },
    {
        "method": "GET",
        "path": "/api/capital-allocation/metrics",
        "handler": "api_get_capital_allocation_metrics", 
        "auth_required": False
    }
]
