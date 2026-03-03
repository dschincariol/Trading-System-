# engine/app.py
"""
Engine entrypoint wrapper.

This repository's stable runtime entry is dashboard_server.py
(which owns HTTP + JobManager).

Keep this file so external tooling that runs `python -m engine.app`
does not break, but do not duplicate orchestration here.
"""

import os
import sys
from dotenv import load_dotenv

# Ensure repo root is importable regardless of current working directory
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)

load_dotenv()


def _validate_env():
    required = ["DATABASE_URL", "SECRET_KEY"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"missing required env vars: {', '.join(missing)}", file=sys.stderr)


def main():
    _validate_env()
    try:
        from dashboard_server import run_server
        run_server()
    except Exception:
        import logging
        logging.critical("unhandled exception in engine.app", exc_info=True)
        # fallback minimal handler
        try:
            from http.server import HTTPServer, BaseHTTPRequestHandler
            class MaintenanceHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"Service unavailable")
            srv = HTTPServer(("", 8000), MaintenanceHandler)
            srv.serve_forever()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
