# start_system.py
"""
Boot entrypoint (legacy)

Production rule:
- No direct subprocess launching here.
- The dashboard server owns orchestration via JobManager APIs.

This file remains as a stable entrypoint wrapper.
"""

import os
import sys

# -------------------------------------------------------------------
# Ensure repo root is importable regardless of current working directory
# (Must run BEFORE any project imports)
# -------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

from dotenv import load_dotenv

# Load .env into process environment
load_dotenv()


def _validate_env():
    # ensure critical environment variables exist or log warnings
    required = ["DATABASE_URL", "SECRET_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        msg = f"missing required env vars: {', '.join(missing)}"
        print(msg, file=sys.stderr)
        # continue but emit warning


def main():
    _validate_env()
    try:
        from dashboard_server import run_server
        run_server()
    except Exception:
        # log and exit with nonzero code; fail early prevents partial startup
        import logging

        logging.critical("unhandled exception in start_system", exc_info=True)
        # safe-mode fallback: start a minimal HTTP server showing 'maintenance'
        try:
            from http.server import HTTPServer, BaseHTTPRequestHandler

            class MaintenanceHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"Service temporarily unavailable")

            srv = HTTPServer(("", 8000), MaintenanceHandler)
            srv.serve_forever()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()