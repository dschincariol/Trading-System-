# server_boot.py
import os
import sys
import json
import time
import threading
import signal

from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# -----------------------------------------------------------------------------
# Process / server lifecycle (double-start safety + shutdown hardening)
# -----------------------------------------------------------------------------

_SERVER_STARTED = False
_SERVER_LOCK = threading.Lock()


def _set_started_once():
    global _SERVER_STARTED
    with _SERVER_LOCK:
        if _SERVER_STARTED:
            raise RuntimeError("server_boot.run_server() called more than once")
        _SERVER_STARTED = True


def _install_signal_handlers(stop_event: threading.Event):
    def _handler(signum, frame):
        try:
            stop_event.set()
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, _handler)
    except Exception:
        pass
    try:
        signal.signal(signal.SIGTERM, _handler)
    except Exception:
        pass

# Ensure static UI paths resolve even when launched from another working directory
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    os.chdir(_BASE_DIR)
except Exception:
    pass

from dev_core.storage import init_db as _init_db

from dashboard_config import (
    HOST,
    PORT,
    DASHBOARD_API_TOKEN,
    AUTO_PIPELINE,
    AUTO_CHALLENGER,
    AUTO_SIZE_POLICY,
    AUTO_PIPELINE_INTERVAL_S,
    AUTO_CHALLENGER_INTERVAL_S,
    AUTO_CHALLENGER_MIN_DRIFT,
    AUTO_SIZE_POLICY_INTERVAL_S,
)

from jobs_manager import (
    JobManager,
    _ensure_job_locks,
    _ensure_job_history,
    _GLOBAL_JOB_MANAGER,
)

from health_checks import run_preflight

from pipeline_runner import (
    LAST_AUTO_PIPELINE_TS,
    LAST_AUTO_CHALLENGER_TS,
    LAST_AUTO_SIZE_POLICY_TS,
    auto_pipeline_loop,
    auto_challenger_loop,
    auto_size_policy_loop,
)

from alerts_service import _ensure_alert_acks, _ensure_alert_resolutions

from api_system import ROUTE_SPECS_SYSTEM
from api_jobs import ROUTE_SPECS_JOBS
from api_ops import ROUTE_SPECS_OPS

from api_handlers import (
    api_get_health,
    api_get_kill_switches,
    api_get_jobs,
    api_get_job_log,
    api_get_job_history,
    api_get_alerts,
    api_get_model_diagnostics,
    api_get_execution_metrics,
    api_get_execution_metrics_rolling,
    api_post_job_start,
    api_post_job_stop,
    api_post_pipeline_run,
)

# Many ops endpoints are implemented in dashboard_server.py (legacy),
# but are still part of ROUTE_SPECS_OPS. Import + adapt them here.
try:
    from dashboard_server import (
        get_model_registry,
        get_embed_model_eval,
        get_embed_conf_calib,
        get_temporal_eval,
        get_temporal_models,
        get_latest_portfolio_backtest,
        get_execution_metrics_by_symbol,
        get_execution_cost_by_confidence,
        get_social_features,
        get_social_regimes,
        get_social_blocks,
        api_get_validation,
        api_get_confidence_mass,
        api_post_rollback,
    )
except Exception:
    get_model_registry = None
    get_embed_model_eval = None
    get_embed_conf_calib = None
    get_temporal_eval = None
    get_temporal_models = None
    get_latest_portfolio_backtest = None
    get_execution_metrics_by_symbol = None
    get_execution_cost_by_confidence = None
    get_social_features = None
    get_social_regimes = None
    get_social_blocks = None
    api_get_validation = None
    api_get_confidence_mass = None
    api_post_rollback = None

ROUTE_SPECS = list(ROUTE_SPECS_SYSTEM) + list(ROUTE_SPECS_JOBS) + list(ROUTE_SPECS_OPS)

def _qs(parsed):
    try:
        q = parse_qs(parsed.query or "", keep_blank_values=True)
        return {k: (v[0] if isinstance(v, list) and v else "") for k, v in q.items()}
    except Exception:
        return {}

def _missing(name: str):
    return {"ok": False, "error": f"handler_unavailable:{name}"}

def _wrap_get_model_registry(parsed, _ctx):
    if not get_model_registry:
        return _missing("get_model_registry")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_model_registry(limit=limit)

def _wrap_get_embed_model_eval(parsed, _ctx):
    if not get_embed_model_eval:
        return _missing("get_embed_model_eval")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "500") or "500")
    return get_embed_model_eval(limit=limit)

def _wrap_get_embed_conf_calib(parsed, _ctx):
    if not get_embed_conf_calib:
        return _missing("get_embed_conf_calib")
    qs = _qs(parsed)
    horizon_s = int(qs.get("horizon_s", "0") or "0")
    model_kind = str(qs.get("model_kind", "") or "")
    limit = int(qs.get("limit", "200") or "200")
    return get_embed_conf_calib(horizon_s=horizon_s, model_kind=model_kind, limit=limit)

def _wrap_get_temporal_eval(parsed, _ctx):
    if not get_temporal_eval:
        return _missing("get_temporal_eval")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_temporal_eval(limit=limit)

def _wrap_get_temporal_models(parsed, _ctx):
    if not get_temporal_models:
        return _missing("get_temporal_models")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "20") or "20")
    return get_temporal_models(limit=limit)

def _wrap_get_latest_portfolio_backtest(_parsed, _ctx):
    if not get_latest_portfolio_backtest:
        return _missing("get_latest_portfolio_backtest")
    return get_latest_portfolio_backtest()

def _wrap_get_execution_metrics_by_symbol(parsed, _ctx):
    if not get_execution_metrics_by_symbol:
        return _missing("get_execution_metrics_by_symbol")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "50") or "50")
    return get_execution_metrics_by_symbol(limit=limit)

def _wrap_get_execution_cost_by_confidence(_parsed, _ctx):
    if not get_execution_cost_by_confidence:
        return _missing("get_execution_cost_by_confidence")
    return get_execution_cost_by_confidence()

def _wrap_get_social_features(parsed, _ctx):
    if not get_social_features:
        return _missing("get_social_features")
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    return get_social_features(symbol=symbol, limit=limit)

def _wrap_get_social_regimes(parsed, _ctx):
    if not get_social_regimes:
        return _missing("get_social_regimes")
    qs = _qs(parsed)
    symbol = str(qs.get("symbol", "") or "").strip()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}
    limit = int(qs.get("limit", "200") or "200")
    return get_social_regimes(symbol=symbol, limit=limit)

def _wrap_get_social_blocks(parsed, _ctx):
    if not get_social_blocks:
        return _missing("get_social_blocks")
    qs = _qs(parsed)
    limit = int(qs.get("limit", "200") or "200")
    return get_social_blocks(limit=limit)

def _wrap_api_get_validation(parsed, _ctx):
    if not api_get_validation:
        return _missing("api_get_validation")
    return api_get_validation(parsed)

def _wrap_api_get_confidence_mass(parsed, _ctx):
    if not api_get_confidence_mass:
        return _missing("api_get_confidence_mass")
    return api_get_confidence_mass(parsed)

def _wrap_api_post_rollback(parsed, body, _ctx):
    if not api_post_rollback:
        return _missing("api_post_rollback")
    return api_post_rollback(parsed, body)


def _ensure_equity_drift():
    from dev_core.storage import connect as _db_connect

    con = _db_connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS equity_drift (
              ts_ms INTEGER PRIMARY KEY,
              diff_equity REAL NOT NULL,
              diff_equity_pct REAL NOT NULL,
              level TEXT NOT NULL
            )
        """
        )
        con.commit()
    finally:
        con.close()


def _ensure_temporal_eval_boot():
    from dev_core.storage import connect as _db_connect

    con = _db_connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS temporal_eval (
              horizon_s INTEGER NOT NULL,
              n INTEGER NOT NULL,
              rmse REAL NOT NULL,
              directional_acc REAL NOT NULL,
              ts_ms INTEGER NOT NULL,
              PRIMARY KEY (ts_ms)
            )
        """
        )
        con.commit()
    finally:
        con.close()


def _ensure_temporal_predictions():
    from dev_core.storage import connect as _db_connect

    con = _db_connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS temporal_predictions (
              event_id INTEGER NOT NULL,
              ts_ms INTEGER NOT NULL,
              horizon_s INTEGER NOT NULL,
              predicted_z REAL NOT NULL,
              created_at_ms INTEGER NOT NULL,
              PRIMARY KEY (event_id, horizon_s)
            )
        """
        )
        con.commit()
    finally:
        con.close()


def _ensure_temporal_models():
    from dev_core.storage import connect as _db_connect

    con = _db_connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS temporal_models (
              model_name TEXT PRIMARY KEY,
              window INTEGER NOT NULL,
              input_dim INTEGER NOT NULL,
              weights BLOB NOT NULL,
              metrics_json TEXT,
              ts_ms INTEGER NOT NULL
            )
        """
        )
        con.commit()
    finally:
        con.close()


def _build_api_handlers():
    return {
        # system
        "api_get_health": api_get_health,
        "api_get_kill_switches": api_get_kill_switches,

        # jobs
        "api_get_jobs": api_get_jobs,
        "api_get_job_log": api_get_job_log,
        "api_get_job_history": api_get_job_history,
        "api_post_job_start": api_post_job_start,
        "api_post_job_stop": api_post_job_stop,
        "api_post_pipeline_run": api_post_pipeline_run,

        # ops (direct)
        "api_get_alerts": api_get_alerts,
        "api_get_model_diagnostics": api_get_model_diagnostics,
        "api_get_execution_metrics": api_get_execution_metrics,
        "api_get_execution_metrics_rolling": api_get_execution_metrics_rolling,

        # ops (legacy dashboard_server functions, adapted to (parsed, ctx))
        "api_get_validation": _wrap_api_get_validation,
        "api_get_confidence_mass": _wrap_api_get_confidence_mass,
        "api_post_rollback": _wrap_api_post_rollback,

        "get_model_registry": _wrap_get_model_registry,
        "get_embed_model_eval": _wrap_get_embed_model_eval,
        "get_embed_conf_calib": _wrap_get_embed_conf_calib,
        "get_temporal_eval": _wrap_get_temporal_eval,
        "get_temporal_models": _wrap_get_temporal_models,
        "get_latest_portfolio_backtest": _wrap_get_latest_portfolio_backtest,
        "get_execution_metrics_by_symbol": _wrap_get_execution_metrics_by_symbol,
        "get_execution_cost_by_confidence": _wrap_get_execution_cost_by_confidence,
        "get_social_features": _wrap_get_social_features,
        "get_social_regimes": _wrap_get_social_regimes,
        "get_social_blocks": _wrap_get_social_blocks,
    }


def _route_coverage_check(routes_map, api_handlers):
    # routes_map: {(METHOD, PATH): "handler_name"}
    expected = set(routes_map.values())
    provided = set(api_handlers.keys())

    missing = sorted([h for h in expected if h not in provided])
    unused = sorted([h for h in provided if h not in expected])

    if missing:
        print("[routes] ERROR missing handlers for ROUTE_SPECS:")
        for h in missing:
            print("  -", h)
        raise RuntimeError("missing api handlers for ROUTE_SPECS")

    if unused:
        print("[routes] WARN api_handlers entries not referenced by ROUTE_SPECS:")
        for h in unused:
            print("  -", h)


class Handler(SimpleHTTPRequestHandler):
    ROUTES = {(m, p): h for (m, p, h) in ROUTE_SPECS}

    _MUTATION_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
    _MUTATION_NAME_PREFIXES = ("api_post_", "api_put_", "api_patch_", "api_delete_", "api_mut_")

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
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _is_localhost_client(self) -> bool:
        try:
            ip = str(self.client_address[0] or "")
            return ip in ("127.0.0.1", "::1")
        except Exception:
            return False

    def _require_mutation_auth(self):
        token = (DASHBOARD_API_TOKEN or "").strip()
        if token:
            hdr = (self.headers.get("X-API-Token") or "").strip()
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

        fn = self.server.API_HANDLERS.get(handler_name)
        if not fn:
            return self.respond_json({"ok": False, "error": f"handler_missing:{handler_name}"}, 500)

        # Auth surface audit: enforce auth on mutation methods
        if method in self._MUTATION_METHODS:
            auth = self._require_mutation_auth()
            if auth:
                return self.respond_json(auth, 403)

        # Method/name guard: prevent accidental exposure if ROUTE_SPECS mismapped handler naming conventions
        if method == "GET" and handler_name.startswith(self._MUTATION_NAME_PREFIXES):
            return self.respond_json({"ok": False, "error": "method_not_allowed"}, 405)

        try:
            if method == "GET":
                return self.respond_json(fn(parsed, self.server.CTX))
            body = self._read_json_body() or {}
            return self.respond_json(fn(parsed, body, self.server.CTX))
        except TypeError:
            try:
                if method == "GET":
                    return self.respond_json(fn(parsed))
                return self.respond_json(fn(parsed, body))
            except Exception as e:
                return self.respond_json({"ok": False, "error": str(e)}, 500)
        except Exception as e:
            return self.respond_json({"ok": False, "error": str(e)}, 500)

    def do_GET(self):
        return self._dispatch()

    def do_POST(self):
        return self._dispatch()


def run_server():
    _set_started_once()

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    try:
        _init_db()
    except Exception as e:
        print(f"[fatal] database init failed: {e}", file=sys.stderr)
        raise

    for fn in (
        _ensure_job_locks,
        _ensure_job_history,
        _ensure_alert_acks,
        _ensure_alert_resolutions,
        _ensure_equity_drift,
        _ensure_temporal_eval_boot,
        _ensure_temporal_predictions,
        _ensure_temporal_models,
    ):
        try:
            fn()
        except Exception:
            pass

    try:
        p = run_preflight()
        if not p.get("ok"):
            print("[preflight] FAILED at startup:")
            for note in p.get("notes", []):
                print("  -", note)
        else:
            print("[preflight] OK")
    except Exception as e:
        print(f"[preflight] exception: {e}")

    JOBS = JobManager(preflight_fn=run_preflight)
    _GLOBAL_JOB_MANAGER.set(JOBS)

    api_handlers = _build_api_handlers()

    # Static route coverage check (ROUTE_SPECS ↔ api_handlers)
    _route_coverage_check(Handler.ROUTES, api_handlers)

    print(f"Dashboard running at http://{HOST}:{PORT}/dashboard.html  (or /ui/dashboard.html)")

    # Thread lifecycle: track threads, daemonized; prevent double-start via _set_started_once()
    threads = []

    if AUTO_PIPELINE:
        print(f"[auto_pipeline] enabled interval_s={AUTO_PIPELINE_INTERVAL_S}")
        t = threading.Thread(target=auto_pipeline_loop, args=(JOBS,), daemon=True)
        t.start()
        threads.append(t)

    if AUTO_CHALLENGER:
        print(
            f"[auto_challenger] enabled interval_s={AUTO_CHALLENGER_INTERVAL_S} drift_gate={AUTO_CHALLENGER_MIN_DRIFT}"
        )
        t = threading.Thread(target=auto_challenger_loop, args=(JOBS,), daemon=True)
        t.start()
        threads.append(t)

    if AUTO_SIZE_POLICY:
        print(f"[auto_size_policy] enabled interval_s={AUTO_SIZE_POLICY_INTERVAL_S}")
        # size policy loop kept in original file previously; keep disabled here unless you re-add it.

    httpd = HTTPServer((HOST, int(PORT)), Handler)
    httpd.API_HANDLERS = api_handlers
    CTX = {
        "JOBS": JOBS,
        "LAST_AUTO_PIPELINE_TS": None,
        "LAST_AUTO_CHALLENGER_TS": None,
        "LAST_AUTO_SIZE_POLICY_TS": None,
    }
    httpd.CTX = CTX

    # Kill-switch / shutdown hardening:
    # - use handle_request loop with timeout so stop_event can end the server promptly
    httpd.timeout = 1.0

    try:
        while not stop_event.is_set():
            httpd.handle_request()
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        try:
            stop_event.set()
        except Exception:
            pass

        try:
            JOBS.stop_all()
        except Exception:
            pass

        try:
            httpd.server_close()
        except Exception:
            pass
