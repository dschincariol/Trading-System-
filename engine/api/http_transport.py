"""
HTTP transport layer only.

Does NOT contain business logic.
Delegates to injected:
- ROUTE_SPECS
- API_HANDLERS
- auth configuration
"""

import json
import os
from http.server import SimpleHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs


def build_handler(ROUTE_SPECS, API_HANDLERS, dashboard_api_token, ctx=None, static_dir=None):
    """
    Builds and returns a configured HTTP request handler class.
    """

    # ------------------------------------------------------------
    # Normalize ROUTE_SPECS (supports dict and tuple formats)
    # ------------------------------------------------------------
    routes = {}

    for r in ROUTE_SPECS:
        # dict style: {"method": "...", "path": "...", "handler": "..."}
        if isinstance(r, dict):
            method = str(r.get("method", "")).upper()
            path = str(r.get("path", ""))
            handler = r.get("handler")
            if method and path and handler:
                routes[(method, path)] = handler
            continue

        # tuple style: (method, path, handler)
        if isinstance(r, tuple) and len(r) >= 3:
            method = str(r[0]).upper()
            path = str(r[1])
            handler = r[2]
            routes[(method, path)] = handler
            continue

    _STATIC_DIR = static_dir or os.getcwd()

    # ------------------------------------------------------------
    # Handler Class
    # ------------------------------------------------------------
    class Handler(SimpleHTTPRequestHandler):

        ROUTES = routes

        def __init__(self, *args, **kwargs):
            self._ctx = ctx or {}
            try:
                super().__init__(*args, directory=_STATIC_DIR, **kwargs)
            except TypeError:
                super().__init__(*args, **kwargs)

            def __init__(self, *args, **kwargs):
                # Pin static serving to repo root so /ui/* never 404s due to CWD drift
                try:
                    super().__init__(*args, directory=_STATIC_DIR, **kwargs)
                except TypeError:
                    # Older Python fallback
                    super().__init__(*args, **kwargs)

        # --------------------------------------------------------
        # Helpers
        # --------------------------------------------------------

        def _normalize_ui_legacy_path(self):
            try:
                parsed = urlparse(self.path)
                if parsed.path in ("/", "/dashboard.html"):
                    self.path = "/ui/dashboard.html"
                    return
                if parsed.path in ("/terminal", "/terminal.html"):
                    self.path = "/ui/terminal/terminal.html"
                    return
            except Exception:
                pass

        def _read_json_body(self):
            try:
                n = int(self.headers.get("Content-Length") or "0")
            except Exception:
                n = 0

            if n <= 0:
                return None

            try:
                raw = self.rfile.read(n)
            except Exception:
                return None

            try:
                return json.loads(raw.decode("utf-8", errors="replace") or "{}")
            except Exception:
                return None

        def respond_json(self, obj, status=200):
            try:
                data = json.dumps(
                    obj,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            except Exception:
                data = b'{"ok":false,"error":"json_encode_failed"}'
                status = 500

            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()

            try:
                self.wfile.write(data)
            except Exception:
                pass

        def _is_localhost_client(self):
            try:
                ip = str(self.client_address[0] or "")
                return ip in ("127.0.0.1", "::1")
            except Exception:
                return False

        def _require_mutation_auth(self):
            token = (dashboard_api_token or "").strip()

            # Token-based auth
            if token:
                try:
                    hdr = (self.headers.get("X-API-Token") or "").strip()
                except Exception:
                    hdr = ""

                if hdr == token:
                    return None

                try:
                    parsed = urlparse(self.path)
                    q = parse_qs(parsed.query)
                    qtok = (q.get("token") or [""])[0]
                except Exception:
                    qtok = ""

                if str(qtok).strip() == token:
                    return None

                return {"ok": False, "error": "unauthorized"}

            # Localhost fallback
            if self._is_localhost_client():
                return None

            return {"ok": False, "error": "forbidden (localhost only)"}

        # --------------------------------------------------------
        # Core Dispatch
        # --------------------------------------------------------

        def _dispatch(self):
            method = str(self.command or "").upper().strip()
            self._normalize_ui_legacy_path()

            parsed = urlparse(self.path)
            key = (method, parsed.path)
            handler_name = self.ROUTES.get(key)

            # No route match → static or 404
            if not handler_name:
                if method == "GET":
                    return super().do_GET()
                return self.respond_json(
                    {"ok": False, "error": "unknown endpoint"},
                    404,
                )

            fn = API_HANDLERS.get(handler_name)
            if not fn:
                return self.respond_json(
                    {"ok": False, "error": f"handler_missing:{handler_name}"},
                    500,
                )

            # Auth required for non-GET
            if method != "GET":
                auth = self._require_mutation_auth()
                if auth:
                    return self.respond_json(auth, 403)

            try:
                body = None
                if method != "GET":
                    body = self._read_json_body() or {}

                # ALWAYS try full 3-arg signature first
                try:
                    result = fn(parsed, body, self._ctx)
                except TypeError:
                    try:
                        # Try 2-arg
                        if body is not None:
                            result = fn(parsed, body)
                        else:
                            result = fn(parsed, self._ctx)
                    except TypeError:
                        # Try 1-arg
                        result = fn(parsed)

                return self.respond_json(result)

            except Exception as e:
                return self.respond_json(
                    {"ok": False, "error": str(e)},
                    500,
                )

        # --------------------------------------------------------
        # HTTP verbs
        # --------------------------------------------------------

        def do_GET(self):
            return self._dispatch()

        def do_POST(self):
            return self._dispatch()

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

    return Handler


def run_http_server(host, port, handler_cls):
    httpd = HTTPServer((host, int(port)), handler_cls)
    return httpd