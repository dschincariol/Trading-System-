# CREATE NEW FILE: api_system.py
# Route specs for system/health endpoints.
# This file contains only route metadata (no runtime imports from dashboard_server.py).

ROUTE_SPECS = [
    ("GET", "/api/health", "api_get_health"),
]