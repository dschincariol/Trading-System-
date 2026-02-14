from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import dashboard_server as legacy
from urllib.parse import urlparse

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _call_legacy(method, path, query, body, request):
    handler_name = legacy.Handler.ROUTES.get((method, path))
    if not handler_name:
        return 404, {"ok": False, "error": "unknown endpoint"}

    fn = legacy.API_HANDLERS.get(handler_name)
    if not fn:
        return 500, {"ok": False, "error": "handler_missing"}

    if method != "GET":
        auth = legacy._require_mutation_auth(request)
        if auth:
            return 403, auth

    parsed = urlparse(path + ("?" + query if query else ""))
    try:
        if method == "GET":
            return 200, fn(parsed)
        return 200, fn(parsed, body or {})
    except Exception as e:
        return 500, {"ok": False, "error": str(e)}

@app.on_event("startup")
def startup():
    legacy.bootstrap_server()

@app.get("/")
def root():
    return RedirectResponse(url="/ui/dashboard.html")

def register_routes():
    for method, path, _ in legacy.ROUTE_SPECS:
        if method == "GET":
            async def handler(request: Request, _path=path):
                status, out = _call_legacy("GET", _path, request.url.query, None, request)
                return JSONResponse(out, status_code=status)
            app.add_api_route(path, handler, methods=["GET"])
        else:
            async def handler(request: Request, _path=path, _method=method):
                body = await request.json()
                status, out = _call_legacy(_method, _path, request.url.query, body, request)
                return JSONResponse(out, status_code=status)
            app.add_api_route(path, handler, methods=[method])

register_routes()
