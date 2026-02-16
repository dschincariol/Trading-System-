// boot/operator_server.js
// Institutional Operator Control Server (Readiness + Typed Config + AutoFix + Persistent Errors)

const express = require("express");
const path = require("path");
const fs = require("fs");
const os = require("os");
const crypto = require("crypto");
const http = require("http");
const https = require("https");
const net = require("net");
const { spawn } = require("child_process");

const app = express();
app.use(express.json());

const ROOT = path.join(__dirname, "..");
const ENTRY = path.join(ROOT, "index.js");
const ENV_PATH = path.join(ROOT, ".env");
const LOG_DIR = path.join(ROOT, "logs");
const RUNTIME_LOG = path.join(LOG_DIR, "runtime.log");
const SECRETS_PATH = path.join(ROOT, "operator.secrets.json");
const STATE_PATH = path.join(ROOT, "operator.state.json");

const OPERATOR_PORT = 4000;
const PRODUCTION_MODE = process.env.NODE_ENV === "production";

let child = null;
let installing = false;

// Persistent state (survives operator restart)
let state = loadState();

// --------------------------------------------------
// Persistent State
// --------------------------------------------------

function defaultState() {
  return {
    createdAt: new Date().toISOString(),
    lastExitCode: null,
    lastError: null,            // { at, kind, message, details? }
    restartAttempts: 0,
    lastStartAt: null,
    lastStopAt: null,
    lastHealthyAt: null,
    lastAutoFix: null           // { at, steps: [...], ok, finalHealth }
  };
}

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
  } catch (e) {
    // last resort: ignore
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
  for (const line of text.split("\n")) {
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

// --------------------------------------------------
// Typed Config Validation + Sanitization
// --------------------------------------------------

const ENV_SPEC = [
  { key: "PORT", type: "int", required: true, min: 1, max: 65535, default: 3000 },
  { key: "DB_PATH", type: "string", required: true, default: "./dev.db" },
  { key: "API_TOKEN", type: "string", required: true, default: "" },

  // Optional but commonly required in your system
  { key: "POLYGON_API_KEY", type: "string", required: false, default: "" },
  { key: "IBKR_HOST", type: "string", required: false, default: "127.0.0.1" },
  { key: "IBKR_PORT", type: "int", required: false, min: 1, max: 65535, default: 4002 },
  { key: "IBKR_CLIENT_ID", type: "int", required: false, min: 0, max: 999999, default: 1 },

  // Operator behavior toggles (optional)
  { key: "OPERATOR_AUTORESTART", type: "bool", required: false, default: true },
  { key: "OPERATOR_HEALTH_URL", type: "string", required: false, default: "" } // override health endpoint
];

function normalizeBool(v) {
  const s = String(v ?? "").trim().toLowerCase();
  if (s === "1" || s === "true" || s === "yes" || s === "y" || s === "on") return true;
  if (s === "0" || s === "false" || s === "no" || s === "n" || s === "off") return false;
  return null;
}

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

  // Extra checks
  const port = Number(sanitized.PORT);
  if (Number.isFinite(port)) {
    // nothing else here
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
    const errMsg = "Config validation failed";
    setLastError("CONFIG_VALIDATION", errMsg, issues);
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
// Dependency Installation (disabled in production)
// --------------------------------------------------

function ensureDependencies(callback) {
  if (PRODUCTION_MODE) return callback();

  if (fs.existsSync(path.join(ROOT, "node_modules"))) {
    return callback();
  }

  if (installing) return;

  installing = true;

  const install = spawn("npm", ["install"], {
    cwd: ROOT,
    stdio: "inherit"
  });

  install.on("exit", (code) => {
    installing = false;
    if (code !== 0) {
      setLastError("NPM_INSTALL_FAILED", "npm install failed", { code });
      return;
    }
    callback();
  });
}

// --------------------------------------------------
// Engine lifecycle
// --------------------------------------------------

function status() {
  if (installing) return "INSTALLING";
  if (child && !child.killed) return "RUNNING";
  return "STOPPED";
}

function startEngine() {
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

  // Write back sanitized values (typed normalization)
  atomicWrite(ENV_PATH, serializeEnv(sanitized));

  ensureDependencies(() => {
    const logStream = fs.createWriteStream(RUNTIME_LOG, { flags: "a" });

    child = spawn("node", [ENTRY], { env: process.env });

    state.lastStartAt = new Date().toISOString();
    saveState();

    child.stdout.pipe(logStream);
    child.stderr.pipe(logStream);

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

    setTimeout(() => startEngine(), 3000);
  }
});

  });

  clearLastError();
  return { ok: true, status: "STARTING" };
}

function stopEngine() {
  if (!child) {
    state.lastStopAt = new Date().toISOString();
    saveState();
    return { ok: true, status: "STOPPED" };
  }

  try {
    child.kill("SIGTERM");
  } catch (e) {
    setLastError("STOP_FAILED", "Failed to stop engine", { message: String(e) });
    return { ok: false };
  }

  state.lastStopAt = new Date().toISOString();
  saveState();
  return { ok: true, status: "STOPPING" };
}

// --------------------------------------------------
// Health verification (backend integration)
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
  const port = Number(env.PORT || 3000);
  const override = String(env.OPERATOR_HEALTH_URL || "").trim();

  // Default: your backend should expose /api/health
  const url = override || `http://localhost:${port}/api/health`;

  const r = await httpGetJson(url);

  if (r.ok) {
    state.lastHealthyAt = new Date().toISOString();
    saveState();
    return { ok: true, url, status: r.status, body: r.json };
  }

  return { ok: false, url, status: r.status, body: r.json };
}

// --------------------------------------------------
// Structured readiness engine
// --------------------------------------------------

async function checkPortAvailable(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once("error", () => resolve(false));
    srv.once("listening", () => srv.close(() => resolve(true)));
    srv.listen(port, "127.0.0.1");
  });
}

async function getReadiness() {
  const issues = [];

  // Files / deps
  if (!fs.existsSync(ENTRY)) issues.push({ level: "error", code: "ENTRY_MISSING", message: "Backend entrypoint missing (index.js)" });
  if (!fs.existsSync(ENV_PATH)) issues.push({ level: "error", code: "ENV_MISSING", message: ".env missing" });
  if (!PRODUCTION_MODE && !fs.existsSync(path.join(ROOT, "node_modules"))) {
    issues.push({ level: "warn", code: "DEPS_MISSING", message: "Dependencies not installed yet (node_modules missing)" });
  }
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

  // Port availability (only if config has a PORT that parses)
  const port = Number(envObj.PORT || "");
  if (Number.isFinite(port) && port > 0 && port <= 65535) {
    const ok = await checkPortAvailable(port);
    if (!ok && status() !== "RUNNING") {
      issues.push({ level: "error", code: "PORT_IN_USE", message: `PORT ${port} appears to be in use` });
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

  // Health (if running)
  let health = null;
  if (status() === "RUNNING") {
    health = await verifyHealth();
    if (!health.ok) {
      issues.push({ level: "warn", code: "HEALTH_FAIL", message: "Backend health check failed" });
    }
  }

  const hasError = issues.some((i) => i.level === "error");
  const ready = !hasError;

  return {
    ready,
    status: status(),
    productionMode: PRODUCTION_MODE,
    issues,
    health
  };
}

// --------------------------------------------------
// Multi-step AutoFix with verification + persistent transcript
// --------------------------------------------------

async function autoFix() {
  const steps = [];
  const startedAt = new Date().toISOString();

  // Step 0: readiness snapshot before
  steps.push({ at: new Date().toISOString(), step: "readiness_before", data: await getReadiness() });

  // Step 1: stop if running
  if (status() === "RUNNING") {
    stopEngine();
    steps.push({ at: new Date().toISOString(), step: "stop_requested" });
    await sleep(1500);
  }

  // Step 2: sanitize config (write back normalized env if valid)
  ensureEnvFile();
  const envObj = readEnv();
  const { sanitized, issues } = validateAndSanitizeEnv(envObj);
  if (issues.some((i) => i.level === "error")) {
    setLastError("AUTOFIX_CONFIG_INVALID", "AutoFix aborted: invalid config", issues);
    steps.push({ at: new Date().toISOString(), step: "config_invalid", data: issues });
    state.lastAutoFix = { at: startedAt, steps, ok: false, finalHealth: null };
    saveState();
    return { ok: false, steps, reason: "config_invalid" };
  }
  atomicWrite(ENV_PATH, serializeEnv(sanitized));
  steps.push({ at: new Date().toISOString(), step: "config_sanitized" });

  // Step 3: install deps (dev only)
  if (!PRODUCTION_MODE && !fs.existsSync(path.join(ROOT, "node_modules"))) {
    steps.push({ at: new Date().toISOString(), step: "deps_install_start" });
    await new Promise((resolve) => {
      ensureDependencies(() => resolve());
      // if ensureDependencies fails, it sets lastError and just returns; detect with timeout
      setTimeout(() => resolve(), 60000);
    });
    steps.push({ at: new Date().toISOString(), step: "deps_install_done" });
  }

  // Step 4: start
  steps.push({ at: new Date().toISOString(), step: "start_requested" });
  const startRes = startEngine();
  if (!startRes.ok) {
    steps.push({ at: new Date().toISOString(), step: "start_failed", data: startRes });
    state.lastAutoFix = { at: startedAt, steps, ok: false, finalHealth: null };
    saveState();
    return { ok: false, steps, reason: "start_failed" };
  }

  // Step 5: wait + verify health (retry loop)
  let finalHealth = null;
  for (let i = 0; i < 8; i++) {
    await sleep(1500);
    finalHealth = await verifyHealth();
    steps.push({ at: new Date().toISOString(), step: "health_check", attempt: i + 1, data: finalHealth });
    if (finalHealth.ok) break;
  }

  const ok = !!(finalHealth && finalHealth.ok);
  if (!ok) {
    setLastError("AUTOFIX_HEALTH_FAIL", "AutoFix completed but health still failing", finalHealth);
  } else {
    clearLastError();
  }

  state.lastAutoFix = { at: startedAt, steps, ok, finalHealth };
  saveState();

  return { ok, steps, finalHealth };
}

// --------------------------------------------------
// Logs
// --------------------------------------------------

function tailLog(lines = 200) {
  if (!fs.existsSync(RUNTIME_LOG)) return "";
  const content = fs.readFileSync(RUNTIME_LOG, "utf-8").split("\n");
  return content.slice(-lines).join("\n");
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
    lastError: state.lastError
  });
});

app.get("/api/operator/bootstrapStatus", (req, res) => {
  res.json({
    nodeVersion: process.version,
    platform: os.platform(),
    productionMode: PRODUCTION_MODE,
    envExists: fs.existsSync(ENV_PATH),
    depsInstalled: fs.existsSync(path.join(ROOT, "node_modules")),
    entryExists: fs.existsSync(ENTRY),
    logDirExists: fs.existsSync(LOG_DIR)
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
  // Return keys only (do not decrypt)
  const secrets = loadSecrets();
  res.json({ keys: Object.keys(secrets) });
});

app.get("/api/operator/logs", (req, res) => {
  res.type("text/plain").send(tailLog(200));
});

app.get("/api/operator/verifyHealth", async (req, res) => {
  const r = await verifyHealth();
  res.json(r);
});

app.get("/api/operator/readiness", async (req, res) => {
  const r = await getReadiness();
  res.json(r);
});

app.post("/api/operator/start", (req, res) => {
  const r = startEngine();
  res.json(r);
});

app.post("/api/operator/stop", (req, res) => {
  const r = stopEngine();
  res.json(r);
});

app.post("/api/operator/restart", async (req, res) => {
  stopEngine();
  await sleep(1000);
  const r = startEngine();
  res.json({ ok: true, status: "RESTARTING", start: r });
});

app.post("/api/operator/autofix", async (req, res) => {
  const r = await autoFix();
  res.json(r);
});

app.get("/api/operator/autofix/last", (req, res) => {
  res.json(state.lastAutoFix || null);
});

app.post("/api/operator/clearLastError", (req, res) => {
  clearLastError();
  res.json({ ok: true });
});

app.post("/api/operator/factoryReset", (req, res) => {
  stopEngine();
  try {
    if (fs.existsSync(SECRETS_PATH)) fs.unlinkSync(SECRETS_PATH);
  } catch {}
  try {
    if (fs.existsSync(ENV_PATH)) fs.unlinkSync(ENV_PATH);
  } catch {}
  try {
    state = defaultState();
    saveState();
  } catch {}
  res.json({ ok: true });
});

app.get("/", (req, res) => {
  res.sendFile(path.join(__dirname, "operator_ui.html"));
});

const OPERATOR_BIND_HOST = "127.0.0.1";

app.listen(OPERATOR_PORT, OPERATOR_BIND_HOST, () => {
  ensureLogDir();
  console.log(`Operator panel running at http://${OPERATOR_BIND_HOST}:${OPERATOR_PORT}`);
});
