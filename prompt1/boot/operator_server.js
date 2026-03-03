// FILE: boot/operator_server.js
// REPLACE THE ENTIRE FILE WITH THIS EXACT CONTENT:

// boot/operator_server.js
// Production Operator Control Center (Non-technical UI)
// - Guided start (Safe / Shadow / Live)
// - Preflight checks (python, port, db writable, entry exists)
// - Readiness + health polling
// - Start/Stop/Restart + Emergency Stop
// - Log tail + snapshot export
// - AutoFix/Repair (pip install, DB touch, quick self-heal)
// - Institutional Check (health + telemetry changes)

const express = require("express");
const path = require("path");
const fs = require("fs");
const os = require("os");
const crypto = require("crypto");
const http = require("http");
const https = require("https");
const net = require("net");
const { spawn, spawnSync } = require("child_process");

const app = express();
app.use(express.json({ limit: "1mb" }));

const ROOT = path.join(__dirname, "..");

// Python dashboard entrypoint (this repo ships start_system.py)
const ENTRY = path.join(ROOT, "start_system.py");

// Files
const ENV_PATH = path.join(ROOT, ".env");
const LOG_DIR = path.join(ROOT, "logs");
const RUNTIME_LOG = path.join(LOG_DIR, "runtime.log");
const SECRETS_PATH = path.join(ROOT, "operator.secrets.json");
const STATE_PATH = path.join(ROOT, "operator.state.json");

// Operator server
const OPERATOR_PORT = Number(process.env.OPERATOR_PORT || 4000);
const OPERATOR_BIND_HOST = String(process.env.OPERATOR_BIND_HOST || "127.0.0.1");
const PRODUCTION_MODE = process.env.NODE_ENV === "production";

let child = null;
let installing = false;

// --------------------------------------------------
// Persistent State
// --------------------------------------------------

function defaultState() {
  return {
    createdAt: new Date().toISOString(),
    lastExitCode: null,
    lastError: null, // { at, kind, message, details? }
    restartAttempts: 0,
    lastStartAt: null,
    lastStopAt: null,
    lastHealthyAt: null,
    lastMode: "safe", // safe | shadow | live
    _restartWindowStart: null,
    _restartCountWindow: 0
  };
}

let state = loadState();

function loadState() {
  try {
    if (!fs.existsSync(STATE_PATH)) return defaultState();
    const obj = JSON.parse(fs.readFileSync(STATE_PATH, "utf-8"));
    return { ...defaultState(), ...obj };
  } catch {
    return defaultState();
  }
}

function saveState() {
  try {
    fs.writeFileSync(STATE_PATH + ".tmp", JSON.stringify(state, null, 2));
    fs.renameSync(STATE_PATH + ".tmp", STATE_PATH);
  } catch {
    // ignore
  }
}

function setLastError(kind, message, details) {
  state.lastError = {
    at: new Date().toISOString(),
    kind,
    message,
    details: details || null
  };
  saveState();
}

function clearLastError() {
  state.lastError = null;
  saveState();
}

// --------------------------------------------------
// Utilities
// --------------------------------------------------

function ensureLogDir() {
  if (!fs.existsSync(LOG_DIR)) fs.mkdirSync(LOG_DIR, { recursive: true });
}

function atomicWrite(file, data) {
  fs.writeFileSync(file + ".tmp", data);
  fs.renameSync(file + ".tmp", file);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function parseEnvText(text) {
  const out = {};
  for (const line of String(text || "").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const idx = trimmed.indexOf("=");
    if (idx === -1) continue;
    const k = trimmed.slice(0, idx).trim();
    const v = trimmed.slice(idx + 1).trim();
    if (k) out[k] = v;
  }
  return out;
}

function serializeEnv(obj) {
  return Object.entries(obj)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

function readEnvFileRaw() {
  if (!fs.existsSync(ENV_PATH)) return "";
  return fs.readFileSync(ENV_PATH, "utf-8");
}

function readEnv() {
  return parseEnvText(readEnvFileRaw());
}

function normalizeBool(v) {
  const s = String(v ?? "").trim().toLowerCase();
  if (s === "1" || s === "true" || s === "yes" || s === "y" || s === "on") return true;
  if (s === "0" || s === "false" || s === "no" || s === "n" || s === "off") return false;
  return null;
}

function nowIso() {
  return new Date().toISOString();
}

function dashBaseUrlFromEnv(envObj) {
  const host = String(envObj.DASHBOARD_HOST || "127.0.0.1");
  const port = Number(envObj.DASHBOARD_PORT || 8000);
  return `http://${host}:${port}`;
}

// --------------------------------------------------
// Typed Config Validation + Sanitization
// --------------------------------------------------

// Keep config aligned with python dashboard defaults.
// NOTE: start_system.py / dashboard_server.py uses 8000 by default.
const ENV_SPEC = [
  { key: "DASHBOARD_HOST", type: "string", required: false, default: "127.0.0.1" },
  { key: "DASHBOARD_PORT", type: "int", required: false, min: 1, max: 65535, default: 8000 },
  { key: "DASHBOARD_API_TOKEN", type: "string", required: false, default: "" },

  { key: "DB_PATH", type: "string", required: false, default: "./trading.db" },

  { key: "POLYGON_API_KEY", type: "string", required: false, default: "" },
  { key: "IBKR_HOST", type: "string", required: false, default: "127.0.0.1" },
  { key: "IBKR_PORT", type: "int", required: false, min: 1, max: 65535, default: 7497 },
  { key: "IBKR_CLIENT_ID", type: "int", required: false, min: 0, max: 999999, default: 101 },

  // Boot behavior
  { key: "AUTO_BOOT_DAEMONS", type: "bool", required: false, default: false },
  { key: "AUTO_BOOT_TARGETS", type: "string", required: false, default: "" },

  // Operator behavior
  { key: "OPERATOR_AUTORESTART", type: "bool", required: false, default: true },
  { key: "OPERATOR_HEALTH_URL", type: "string", required: false, default: "" }
];

function validateAndSanitizeEnv(envObj) {
  const sanitized = { ...envObj };
  const issues = [];

  for (const spec of ENV_SPEC) {
    const raw = sanitized[spec.key];

    if (raw === undefined || raw === null || String(raw).trim() === "") {
      if (spec.required) {
        issues.push({ key: spec.key, level: "error", message: "Missing required value" });
      }
      if (spec.default !== undefined) {
        sanitized[spec.key] = String(spec.default);
      }
      continue;
    }

    if (spec.type === "string") {
      sanitized[spec.key] = String(raw).trim();
      continue;
    }

    if (spec.type === "int") {
      const n = Number(String(raw).trim());
      if (!Number.isFinite(n) || !Number.isInteger(n)) {
        issues.push({ key: spec.key, level: "error", message: "Must be an integer" });
        continue;
      }
      if (spec.min !== undefined && n < spec.min) {
        issues.push({ key: spec.key, level: "error", message: `Must be >= ${spec.min}` });
        continue;
      }
      if (spec.max !== undefined && n > spec.max) {
        issues.push({ key: spec.key, level: "error", message: `Must be <= ${spec.max}` });
        continue;
      }
      sanitized[spec.key] = String(n);
      continue;
    }

    if (spec.type === "bool") {
      const b = normalizeBool(raw);
      if (b === null) {
        issues.push({ key: spec.key, level: "error", message: "Must be a boolean (true/false)" });
        continue;
      }
      sanitized[spec.key] = b ? "true" : "false";
      continue;
    }
  }

  return { sanitized, issues };
}

function ensureEnvFile() {
  if (fs.existsSync(ENV_PATH)) return;

  const base = {};
  for (const spec of ENV_SPEC) {
    if (spec.default !== undefined) base[spec.key] = String(spec.default);
  }
  atomicWrite(ENV_PATH, serializeEnv(base));
}

function writeEnv(obj) {
  const { sanitized, issues } = validateAndSanitizeEnv(obj);
  if (issues.some((i) => i.level === "error")) {
    setLastError("CONFIG_VALIDATION", "Config validation failed", issues);
    return { ok: false, issues, sanitized };
  }
  atomicWrite(ENV_PATH, serializeEnv(sanitized));
  return { ok: true, issues: [], sanitized };
}

// --------------------------------------------------
// Secrets (AES-256-GCM) + Atomic writes
// --------------------------------------------------

const MASTER_KEY = crypto.createHash("sha256").update(os.hostname()).digest();

function encrypt(text) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", MASTER_KEY, iv);
  const enc = Buffer.concat([cipher.update(String(text), "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([iv, tag, enc]).toString("base64");
}

function decrypt(enc) {
  const buf = Buffer.from(enc, "base64");
  const iv = buf.slice(0, 12);
  const tag = buf.slice(12, 28);
  const data = buf.slice(28);
  const decipher = crypto.createDecipheriv("aes-256-gcm", MASTER_KEY, iv);
  decipher.setAuthTag(tag);
  return decipher.update(data, null, "utf8") + decipher.final("utf8");
}

function loadSecrets() {
  if (!fs.existsSync(SECRETS_PATH)) return {};
  try {
    return JSON.parse(fs.readFileSync(SECRETS_PATH, "utf-8"));
  } catch {
    return {};
  }
}

function saveSecrets(obj) {
  atomicWrite(SECRETS_PATH, JSON.stringify(obj, null, 2));
}

// --------------------------------------------------
// Engine lifecycle
// --------------------------------------------------

function status() {
  if (installing) return "INSTALLING";
  if (child && !child.killed) return "RUNNING";
  return "STOPPED";
}

function pickPythonCmd() {
  // On Windows, "py" is common. Keep "python" first for consistency.
  const candidates = [process.env.OPERATOR_PYTHON || "python", "py"];
  for (const c of candidates) {
    try {
      const r = spawnSync(c, ["--version"], { stdio: "pipe" });
      if (r && (r.status === 0 || r.status === null)) return c;
    } catch {}
  }
  return candidates[0];
}

function startEngine(mode = "safe") {
  if (child) return { ok: true, status: "RUNNING" };

  ensureEnvFile();
  ensureLogDir();

  // Validate env before start
  const current = readEnv();
  const { sanitized, issues } = validateAndSanitizeEnv(current);
  if (issues.some((i) => i.level === "error")) {
    setLastError("CONFIG_INVALID", "Cannot start: invalid .env", issues);
    return { ok: false, status: "STOPPED", issues };
  }

  // Apply boot mode overrides (non-technical toggle)
  const m = String(mode || "safe").toLowerCase().trim();
  const finalMode = (m === "live" || m === "shadow") ? m : "safe";

  // safe: UI only, no jobs
  // shadow/live: auto boot daemons
  sanitized.AUTO_BOOT_DAEMONS = (finalMode === "safe") ? "false" : "true";
  sanitized.EXECUTION_MODE = finalMode; // if your backend/UI uses it
  sanitized.OPERATOR_MODE = finalMode;  // reserved

  atomicWrite(ENV_PATH, serializeEnv(sanitized));

  const python = pickPythonCmd();

  const logStream = fs.createWriteStream(RUNTIME_LOG, { flags: "a" });
  logStream.write(`\n[${nowIso()}] OPERATOR start mode=${finalMode} python=${python}\n`);

  child = spawn(python, [ENTRY], { env: { ...process.env, ...sanitized }, cwd: ROOT });

  state.lastStartAt = nowIso();
  state.lastMode = finalMode;
  saveState();

  if (child.stdout) child.stdout.pipe(logStream);
  if (child.stderr) child.stderr.pipe(logStream);

  child.on("exit", (code) => {
    state.lastExitCode = code;
    saveState();
    child = null;

    const envNow = readEnv();
    const ar = normalizeBool(envNow.OPERATOR_AUTORESTART);
    const autoRestartEnabled = ar === null ? true : ar;

    const now = Date.now();
    if (!state._restartWindowStart) {
      state._restartWindowStart = now;
      state._restartCountWindow = 0;
    }

    // Reset window if older than 10 minutes
    if (now - state._restartWindowStart > 10 * 60 * 1000) {
      state._restartWindowStart = now;
      state._restartCountWindow = 0;
    }

    if (autoRestartEnabled && code !== 0) {
      state._restartCountWindow += 1;
      state.restartAttempts = (state.restartAttempts || 0) + 1;

      if (state._restartCountWindow > 5) {
        setLastError(
          "CRASH_LOOP_DETECTED",
          "Engine crashed too many times within 10 minutes. Auto-restart disabled."
        );
        saveState();
        return;
      }

      setLastError("ENGINE_CRASH", "Engine exited unexpectedly", { code });
      saveState();

      setTimeout(() => startEngine(state.lastMode || "safe"), 3000);
    }
  });

  clearLastError();
  return { ok: true, status: "STARTING", mode: finalMode };
}

function stopEngine() {
  if (!child) {
    state.lastStopAt = nowIso();
    saveState();
    return { ok: true, status: "STOPPED" };
  }

  try {
    child.kill("SIGTERM");
  } catch (e) {
    setLastError("STOP_FAILED", "Failed to stop engine", { message: String(e) });
    return { ok: false };
  }

  state.lastStopAt = nowIso();
  saveState();
  return { ok: true, status: "STOPPING" };
}

function emergencyStop() {
  // Strong stop: SIGKILL if still alive after grace period
  if (!child) return { ok: true, status: "STOPPED" };
  try {
    child.kill("SIGTERM");
  } catch {}
  const pid = child.pid;
  setTimeout(() => {
    try {
      if (child && child.pid === pid) child.kill("SIGKILL");
    } catch {}
  }, 2500);
  state.lastStopAt = nowIso();
  saveState();
  return { ok: true, status: "STOPPING" };
}

// --------------------------------------------------
// Health + Readiness (backend integration)
// --------------------------------------------------

function httpGetJson(url) {
  return new Promise((resolve) => {
    try {
      const lib = url.startsWith("https://") ? https : http;
      const req = lib.get(url, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => {
          try {
            const obj = JSON.parse(data || "{}");
            resolve({ ok: res.statusCode >= 200 && res.statusCode < 300, status: res.statusCode, json: obj });
          } catch {
            resolve({ ok: false, status: res.statusCode, json: null });
          }
        });
      });
      req.on("error", () => resolve({ ok: false, status: 0, json: null }));
      req.setTimeout(2500, () => {
        req.destroy();
        resolve({ ok: false, status: 0, json: null });
      });
    } catch {
      resolve({ ok: false, status: 0, json: null });
    }
  });
}

async function verifyHealth() {
  const env = readEnv();
  const port = Number(env.DASHBOARD_PORT || 8000);
  const host = String(env.DASHBOARD_HOST || "127.0.0.1");
  const override = String(env.OPERATOR_HEALTH_URL || "").trim();

  const url = override || `http://${host}:${port}/api/health`;

  const r = await httpGetJson(url);

  if (r.ok) {
    state.lastHealthyAt = nowIso();
    saveState();
    return { ok: true, url, status: r.status, body: r.json };
  }

  return { ok: false, url, status: r.status, body: r.json };
}

async function checkPortAvailable(port, host = "127.0.0.1") {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once("error", () => resolve(false));
    srv.once("listening", () => srv.close(() => resolve(true)));
    srv.listen(port, host);
  });
}

function isPathWritable(p) {
  try {
    // If p is a file path that doesn't exist yet, check its parent directory.
    let target = p;

    try {
      const st = fs.statSync(p);
      if (!st.isDirectory()) target = path.dirname(p);
    } catch {
      target = path.dirname(p);
    }

    fs.accessSync(target, fs.constants.W_OK);
    return true;
  } catch {
    return false;
  }
}

function pythonAvailable() {
  const python = pickPythonCmd();
  try {
    const r = spawnSync(python, ["--version"], { stdio: "pipe" });
    const out = (r.stdout ? String(r.stdout) : "") + (r.stderr ? String(r.stderr) : "");
    const ok = r.status === 0 || r.status === null;
    return { ok, python, version: out.trim() };
  } catch (e) {
    return { ok: false, python, version: "", error: String(e) };
  }
}

function resolveDbPathFromSanitized(sanitized) {
  const dbPath = String(sanitized.DB_PATH || "./trading.db");
  const resolvedDb = path.isAbsolute(dbPath) ? dbPath : path.join(ROOT, dbPath);
  return { dbPath, resolvedDb };
}

async function getPreflight(mode = "safe") {
  ensureEnvFile();

  const envObj = readEnv();
  const { sanitized, issues: cfgIssues } = validateAndSanitizeEnv(envObj);

  // Persist normalized env (only if no errors)
  if (!cfgIssues.some((i) => i.level === "error")) {
    atomicWrite(ENV_PATH, serializeEnv(sanitized));
  }

  const checks = [];

  // Entry exists
  checks.push({
    id: "entry",
    label: "Backend entrypoint exists",
    ok: fs.existsSync(ENTRY),
    details: ENTRY
  });

  // Python exists
  const py = pythonAvailable();
  checks.push({
    id: "python",
    label: "Python available",
    ok: !!py.ok,
    details: py.ok ? `${py.python} (${py.version || "ok"})` : (py.error || "not found")
  });

  // Dashboard port available (if not running)
  const dashPort = Number(sanitized.DASHBOARD_PORT || 8000);
  const dashHost = String(sanitized.DASHBOARD_HOST || "127.0.0.1");
  if (Number.isFinite(dashPort) && dashPort > 0 && dashPort <= 65535) {
    const portOk = await checkPortAvailable(dashPort, dashHost);
    checks.push({
      id: "port",
      label: `Dashboard port available (${dashHost}:${dashPort})`,
      ok: portOk || status() === "RUNNING",
      details: portOk ? "free" : (status() === "RUNNING" ? "engine running" : "in use")
    });
  } else {
    checks.push({
      id: "port",
      label: "Dashboard port configured",
      ok: false,
      details: "invalid DASHBOARD_PORT"
    });
  }

  // DB path writable
  const { dbPath, resolvedDb } = resolveDbPathFromSanitized(sanitized);
  checks.push({
    id: "db",
    label: "DB path writable",
    ok: isPathWritable(resolvedDb),
    details: resolvedDb
  });

  // Config validity
  const cfgOk = !cfgIssues.some((i) => i.level === "error");
  checks.push({
    id: "config",
    label: ".env config valid",
    ok: cfgOk,
    details: cfgOk ? "ok" : cfgIssues.map((x) => `${x.key}: ${x.message}`).join("; ")
  });

  // Live/shadow key requirements (soft check)
  const m = String(mode || "safe").toLowerCase().trim();
  const wantData = (m === "shadow" || m === "live");
  if (wantData) {
    checks.push({
      id: "polygon_key",
      label: "POLYGON_API_KEY present (data feed)",
      ok: !!String(sanitized.POLYGON_API_KEY || "").trim(),
      details: String(sanitized.POLYGON_API_KEY || "").trim() ? "set" : "missing"
    });
  }

  // IBKR fields (soft check)
  if (m === "live") {
    checks.push({
      id: "ibkr_host",
      label: "IBKR_HOST configured",
      ok: !!String(sanitized.IBKR_HOST || "").trim(),
      details: String(sanitized.IBKR_HOST || "").trim() || "missing"
    });
  }

  const ok = checks.every((c) => !!c.ok);

  return {
    ok,
    mode: m,
    status: status(),
    productionMode: PRODUCTION_MODE,
    checks,
    configIssues: cfgIssues
  };
}

async function getReadiness() {
  const issues = [];

  // Files
  if (!fs.existsSync(ENTRY)) issues.push({ level: "error", code: "ENTRY_MISSING", message: `Backend entrypoint missing (${path.basename(ENTRY)})` });
  if (!fs.existsSync(ENV_PATH)) issues.push({ level: "error", code: "ENV_MISSING", message: ".env missing" });
  if (!fs.existsSync(LOG_DIR)) issues.push({ level: "warn", code: "LOGDIR_MISSING", message: "logs/ folder missing (will be created on start)" });

  // Config validity
  const envObj = readEnv();
  const { issues: cfgIssues } = validateAndSanitizeEnv(envObj);
  for (const ci of cfgIssues) {
    issues.push({
      level: ci.level === "error" ? "error" : "warn",
      code: "CONFIG_" + ci.key,
      message: `${ci.key}: ${ci.message}`
    });
  }

  // Health (if running)
  let health = null;
  if (status() === "RUNNING") {
    health = await verifyHealth();
    if (!health.ok) {
      issues.push({ level: "warn", code: "HEALTH_FAIL", message: "Backend health check failed" });
    }
  }

  // Last error persistence
  if (state.lastError) {
    issues.push({
      level: "warn",
      code: "LAST_ERROR_PRESENT",
      message: `Last error: ${state.lastError.kind} — ${state.lastError.message}`
    });
  }

  const hasError = issues.some((i) => i.level === "error");
  return {
    ready: !hasError,
    status: status(),
    productionMode: PRODUCTION_MODE,
    mode: state.lastMode || "safe",
    issues,
    health
  };
}

// --------------------------------------------------
// Logs + Snapshot
// --------------------------------------------------

function tailLog(lines = 200) {
  if (!fs.existsSync(RUNTIME_LOG)) return "";
  const content = fs.readFileSync(RUNTIME_LOG, "utf-8").split("\n");
  return content.slice(-lines).join("\n");
}

function safeEnvForSnapshot(envObj) {
  const out = { ...envObj };
  const redact = ["POLYGON_API_KEY", "DASHBOARD_API_TOKEN", "API_TOKEN", "IBKR_PASSWORD", "IBKR_TOKEN"];
  for (const k of redact) {
    if (out[k]) out[k] = "***REDACTED***";
  }
  return out;
}

// --------------------------------------------------
// AutoFix / Repair
// --------------------------------------------------

function runPipInstallRequirements() {
  const python = pickPythonCmd();
  const reqPath = path.join(ROOT, "requirements.txt");
  if (!fs.existsSync(reqPath)) {
    return { ok: false, kind: "REQ_MISSING", message: "requirements.txt missing", details: reqPath };
  }

  const r = spawnSync(python, ["-m", "pip", "install", "-r", reqPath], {
    cwd: ROOT,
    stdio: "pipe",
    env: process.env
  });

  const out = (r.stdout ? String(r.stdout) : "") + (r.stderr ? String(r.stderr) : "");
  const ok = r.status === 0;

  return { ok, kind: ok ? "PIP_OK" : "PIP_FAIL", message: ok ? "pip install -r requirements.txt ok" : "pip install failed", details: out.trim() };
}

function touchDbFile(resolvedDb) {
  try {
    const dir = path.dirname(resolvedDb);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    if (!fs.existsSync(resolvedDb)) fs.writeFileSync(resolvedDb, "");
    return { ok: true, kind: "DB_OK", message: "DB file present", details: resolvedDb };
  } catch (e) {
    return { ok: false, kind: "DB_FAIL", message: "Could not create DB file", details: String(e) };
  }
}

// --------------------------------------------------
// Institutional Check (health + telemetry changes)
// --------------------------------------------------

async function checkTelemetryFlow() {
  const envObj = readEnv();
  const base = dashBaseUrlFromEnv(envObj);
  const url = `${base}/api/telemetry`;

  const a = await httpGetJson(url);
  if (!a.ok) return { ok: false, url, detail: "telemetry not responding" };

  await sleep(1200);

  const b = await httpGetJson(url);
  if (!b.ok) return { ok: false, url, detail: "telemetry not responding (second sample)" };

  const sa = JSON.stringify(a.json || {});
  const sb = JSON.stringify(b.json || {});
  const changed = sa !== sb;

  return { ok: changed, url, detail: changed ? "telemetry changed" : "telemetry unchanged" };
}

// --------------------------------------------------
// API
// --------------------------------------------------

app.get("/api/operator/status", (req, res) => {
  res.json({
    status: status(),
    installing,
    productionMode: PRODUCTION_MODE,
    lastExitCode: state.lastExitCode,
    restartAttempts: state.restartAttempts,
    lastStartAt: state.lastStartAt,
    lastStopAt: state.lastStopAt,
    lastHealthyAt: state.lastHealthyAt,
    lastMode: state.lastMode || "safe",
    lastError: state.lastError
  });
});

app.get("/api/operator/bootstrap", (req, res) => {
  res.json({
    nodeVersion: process.version,
    platform: os.platform(),
    productionMode: PRODUCTION_MODE,
    envExists: fs.existsSync(ENV_PATH),
    entryExists: fs.existsSync(ENTRY),
    logDirExists: fs.existsSync(LOG_DIR),
    operator: { host: OPERATOR_BIND_HOST, port: OPERATOR_PORT }
  });
});

// UI expects this endpoint
app.get("/api/operator/bootstrapStatus", async (req, res) => {
  const envObj = readEnv();
  const readiness = await getReadiness();
  const preflight = await getPreflight(state.lastMode || "safe");
  const health = (status() === "RUNNING") ? await verifyHealth() : null;

  res.json({
    at: nowIso(),
    operator: {
      host: OPERATOR_BIND_HOST,
      port: OPERATOR_PORT,
      node: process.version,
      platform: os.platform(),
      productionMode: PRODUCTION_MODE
    },
    engine: {
      status: status(),
      entry: ENTRY,
      lastMode: state.lastMode || "safe",
      lastStartAt: state.lastStartAt,
      lastStopAt: state.lastStopAt,
      lastHealthyAt: state.lastHealthyAt,
      lastExitCode: state.lastExitCode,
      lastError: state.lastError,
      restartAttempts: state.restartAttempts
    },
    dashboard: { baseUrl: dashBaseUrlFromEnv(envObj) },
    readiness,
    preflight,
    health
  });
});

app.get("/api/operator/config", (req, res) => {
  res.type("application/json").send(JSON.stringify(readEnv(), null, 2));
});

app.post("/api/operator/config", (req, res) => {
  const result = writeEnv(req.body || {});
  res.json(result);
});

app.get("/api/operator/config/validate", (req, res) => {
  const envObj = readEnv();
  const { sanitized, issues } = validateAndSanitizeEnv(envObj);
  res.json({ ok: !issues.some((i) => i.level === "error"), issues, sanitized });
});

app.post("/api/operator/secrets", (req, res) => {
  const secrets = loadSecrets();
  for (const k in (req.body || {})) {
    secrets[k] = encrypt(req.body[k]);
  }
  saveSecrets(secrets);
  res.json({ ok: true });
});

app.get("/api/operator/secrets", (req, res) => {
  const secrets = loadSecrets();
  res.json({ keys: Object.keys(secrets) });
});

app.get("/api/operator/logs", (req, res) => {
  const n = Math.max(50, Math.min(2000, Number(req.query.lines || 400)));
  res.type("text/plain").send(tailLog(n));
});

app.get("/api/operator/verifyHealth", async (req, res) => {
  const r = await verifyHealth();
  res.json(r);
});

app.get("/api/operator/readiness", async (req, res) => {
  const r = await getReadiness();
  res.json(r);
});

app.get("/api/operator/preflight", async (req, res) => {
  const mode = String(req.query.mode || state.lastMode || "safe");
  const r = await getPreflight(mode);
  res.json(r);
});

// UI expects this endpoint
app.get("/api/operator/institutionalCheck", async (req, res) => {
  ensureEnvFile();
  const envObj = readEnv();
  const { sanitized, issues } = validateAndSanitizeEnv(envObj);

  const entryExists = fs.existsSync(ENTRY);
  const configValid = !issues.some((i) => i.level === "error");

  const { resolvedDb } = resolveDbPathFromSanitized(sanitized);
  const dbPathWritable = isPathWritable(resolvedDb);

  const health = (status() === "RUNNING") ? await verifyHealth() : { ok: false };
  const healthOk = !!(health && health.ok);

  const telemetry = (status() === "RUNNING") ? await checkTelemetryFlow() : { ok: false, detail: "engine not running" };
  const dataFlowing = !!(telemetry && telemetry.ok);

  const errors = [];
  if (!configValid) errors.push("config invalid");
  if (!entryExists) errors.push("entry missing");
  if (!dbPathWritable) errors.push("db not writable");
  if (!healthOk) errors.push("health not ok");
  if (!dataFlowing) errors.push("telemetry not changing");

  res.json({
    ok: configValid && entryExists && healthOk && dataFlowing,
    configValid,
    entryExists,
    dbPathWritable,
    healthOk,
    dataFlowing,
    details: {
      dashboardBase: dashBaseUrlFromEnv(sanitized),
      telemetry: telemetry || null,
      health: health || null,
      resolvedDb
    },
    errors
  });
});

// UI calls this (AutoFix/Repair)
app.post("/api/operator/autofix", async (req, res) => {
  const steps = [];
  try {
    ensureEnvFile();
    ensureLogDir();

    // Normalize env first
    const envObj = readEnv();
    const { sanitized, issues } = validateAndSanitizeEnv(envObj);
    if (!issues.some((i) => i.level === "error")) {
      atomicWrite(ENV_PATH, serializeEnv(sanitized));
      steps.push({ ok: true, kind: "ENV_OK", message: "Normalized .env", details: ENV_PATH });
    } else {
      steps.push({ ok: false, kind: "ENV_INVALID", message: "Config has validation errors", details: issues });
    }

    // pip install
    steps.push(runPipInstallRequirements());

    // touch DB
    const { resolvedDb } = resolveDbPathFromSanitized(sanitized || envObj || {});
    steps.push(touchDbFile(resolvedDb));

    // clear last error
    clearLastError();
    steps.push({ ok: true, kind: "CLEAR_ERROR", message: "Cleared last error", details: null });

    const ok = steps.every((s) => !!s.ok);
    if (!ok) setLastError("AUTOFIX_PARTIAL", "AutoFix could not complete all steps", steps);

    return res.json({ ok, steps });
  } catch (e) {
    setLastError("AUTOFIX_FAIL", "AutoFix failed", { message: String(e) });
    return res.json({ ok: false, steps, error: String(e) });
  }
});

app.post("/api/operator/start", async (req, res) => {
  const mode = String((req.body && req.body.mode) || "safe");
  const steps = [];

  steps.push({ id: "preflight", ok: true, label: "Preflight checks", detail: "running" });
  const pre = await getPreflight(mode);
  if (!pre.ok) {
    steps[steps.length - 1] = { id: "preflight", ok: false, label: "Preflight checks", detail: pre.checks };
    setLastError("PREFLIGHT_FAIL", "Preflight checks failed; cannot start", pre.checks);
    return res.json({ ok: false, status: "STOPPED", steps, preflight: pre });
  }
  steps[steps.length - 1] = { id: "preflight", ok: true, label: "Preflight checks", detail: "ok" };

  steps.push({ id: "spawn", ok: true, label: "Launching backend", detail: "starting python" });
  const r = startEngine(mode);
  if (!r.ok) {
    steps[steps.length - 1] = { id: "spawn", ok: false, label: "Launching backend", detail: r };
    setLastError("START_FAIL", "Could not start backend", r);
    return res.json({ ok: false, status: "STOPPED", steps });
  }
  steps[steps.length - 1] = { id: "spawn", ok: true, label: "Launching backend", detail: `started (${mode})` };

  // Wait for health
  steps.push({ id: "health", ok: false, label: "Waiting for health", detail: "polling /api/health" });
  let healthy = false;
  let lastHealth = null;
  for (let i = 0; i < 10; i++) {
    await sleep(1000);
    lastHealth = await verifyHealth();
    if (lastHealth.ok) {
      healthy = true;
      break;
    }
  }

  if (!healthy) {
    steps[steps.length - 1] = { id: "health", ok: false, label: "Waiting for health", detail: lastHealth || "not healthy" };
    setLastError("BOOT_HEALTH_FAIL", "Backend did not become healthy");
    return res.json({ ok: false, status: "UNHEALTHY", steps });
  }
  steps[steps.length - 1] = { id: "health", ok: true, label: "Waiting for health", detail: "healthy" };

  // Data verification: telemetry responds
  steps.push({ id: "telemetry", ok: false, label: "Checking telemetry", detail: "polling /api/telemetry" });
  const envObj = readEnv();
  const base = dashBaseUrlFromEnv(envObj);
  const dbCheck = await httpGetJson(`${base}/api/telemetry`);

  if (!dbCheck.ok) {
    steps[steps.length - 1] = { id: "telemetry", ok: false, label: "Checking telemetry", detail: "telemetry not responding" };
    setLastError("DATA_API_FAIL", "Telemetry API not responding");
    return res.json({ ok: false, status: "NO_DATA_API", steps });
  }
  steps[steps.length - 1] = { id: "telemetry", ok: true, label: "Checking telemetry", detail: "telemetry responding" };

  return res.json({ ok: true, status: "RUNNING", mode, steps });
});

app.post("/api/operator/stop", (req, res) => {
  const r = stopEngine();
  res.json(r);
});

app.post("/api/operator/restart", async (req, res) => {
  stopEngine();
  await sleep(1000);
  const mode = String((req.body && req.body.mode) || state.lastMode || "safe");
  const r = startEngine(mode);
  res.json({ ok: true, status: "RESTARTING", start: r });
});

app.post("/api/operator/emergencyStop", (req, res) => {
  const r = emergencyStop();
  res.json(r);
});

app.post("/api/operator/clearLastError", (req, res) => {
  clearLastError();
  res.json({ ok: true });
});

app.get("/api/operator/snapshot", async (req, res) => {
  const envObj = readEnv();
  const readiness = await getReadiness();
  const preflight = await getPreflight(state.lastMode || "safe");

  const snap = {
    at: nowIso(),
    operator: {
      host: OPERATOR_BIND_HOST,
      port: OPERATOR_PORT,
      node: process.version,
      platform: os.platform(),
      productionMode: PRODUCTION_MODE
    },
    engine: {
      status: status(),
      entry: ENTRY,
      lastMode: state.lastMode || "safe",
      lastStartAt: state.lastStartAt,
      lastStopAt: state.lastStopAt,
      lastHealthyAt: state.lastHealthyAt,
      lastExitCode: state.lastExitCode,
      lastError: state.lastError,
      restartAttempts: state.restartAttempts
    },
    readiness,
    preflight,
    env: safeEnvForSnapshot(envObj),
    logsTail: tailLog(250)
  };

  res.setHeader("Content-Type", "application/json");
  res.setHeader("Content-Disposition", `attachment; filename="operator_snapshot_${Date.now()}.json"`);
  res.send(JSON.stringify(snap, null, 2));
});

app.post("/api/operator/factoryReset", (req, res) => {
  emergencyStop();
  try { if (fs.existsSync(SECRETS_PATH)) fs.unlinkSync(SECRETS_PATH); } catch {}
  try { if (fs.existsSync(ENV_PATH)) fs.unlinkSync(ENV_PATH); } catch {}
  try { state = defaultState(); saveState(); } catch {}
  res.json({ ok: true });
});

// UI
app.get("/", (req, res) => {
  res.sendFile(path.join(__dirname, "operator_ui.html"));
});

app.listen(OPERATOR_PORT, OPERATOR_BIND_HOST, () => {
  ensureLogDir();
  console.log(`Operator Control Center: http://${OPERATOR_BIND_HOST}:${OPERATOR_PORT}`);
});