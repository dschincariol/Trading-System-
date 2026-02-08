# CREATE NEW FILE: api_system.py
# Route specs for system/health endpoints.
# This file contains only route metadata (no runtime imports from dashboard_server.py).

ROUTE_SPECS = [
    ("GET", "/api/system/kill_switches", "api_get_kill_switches"),
    ("GET", "/api/health", "api_get_health"),
]

ROUTE_SPECS_SYSTEM = ROUTE_SPECS
