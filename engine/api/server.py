# engine/api/server.py
"""
HTTP entrypoint shim.

Master prompt requirement:
- Single canonical HTTP server + route registration source.
- Retain legacy import compatibility for tools that reference engine.api.server.

Canonical server: dashboard_server.py
"""

from __future__ import annotations


def run_server():
    from dashboard_server import run_server as _run

    return _run()


def main():
    return run_server()


if __name__ == "__main__":
    main()
