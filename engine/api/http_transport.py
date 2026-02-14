# engine/api/http_transport.py
"""
HTTP transport layer only.

Does NOT contain business logic.
Delegates to injected:
- ROUTE_SPECS
- API_HANDLERS
- auth configuration
"""

import json
from http.server import SimpleHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs


def build_handler(ROUTE_SPECS, API_HANDLERS, dashboard_api_token, ctx=None):

    routes = {(m, p): h for (m, p, h) in ROUTE_SPECS}

    class Handler(SimpleHTTPRequestHandler):

        ROUTES = routes
        CTX = ctx or {}


        def _normalize_ui_legacy_path(self):
            try:
                parsed = urlparse(self.path)
                if parsed.path in ("/", "/dashboard.html"):
                    self.path = "/ui/dashboard.html"
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
                data = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")
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

            if self._is_localhost_client():
                return None

            return {"ok": False, "error": "forbidden (localhost only)"}

        def _dispatch(self):
            method = str(self.command or "").upper().strip()
            self._normalize_ui_legacy_path()

            parsed = urlparse(self.path)
            key = (method, parsed.path)
            handler_name = self.ROUTES.get(key)

            if not handler_name:
                if method == "GET":
                    return super().do_GET()
                return self.respond_json({"ok": False, "error": "unknown endpoint"}, 404)

            fn = API_HANDLERS.get(handler_name)
            if not fn:
                return self.respond_json({"ok": False, "error": f"handler_missing:{handler_name}"}, 500)

            if method != "GET":
                auth = self._require_mutation_auth()
                if auth:
                    return self.respond_json(auth, 403)

            try:
                # Flexible call signatures:
                # GET:
                #   fn(parsed)
                #   fn(parsed, ctx)
                #
                # POST:
                #   fn(parsed, body)
                #   fn(parsed, body, ctx)

                if method == "GET":
                    try:
                        return self.respond_json(fn(parsed, self.CTX))
                    except TypeError:
                        return self.respond_json(fn(parsed))

                body = self._read_json_body() or {}

                try:
                    return self.respond_json(fn(parsed, body, self.CTX))
                except TypeError:
                    return self.respond_json(fn(parsed, body))

            except Exception as e:
                return self.respond_json({"ok": False, "error": str(e)}, 500)

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
