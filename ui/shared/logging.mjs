import fs from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { format } from "node:util";

import { isSecretName, redactLogText, registerLogSecrets } from "./log-redaction.mjs";
export { redactLogText, registerLogSecrets } from "./log-redaction.mjs";

const files = new Set(["backend", "web", "tui"]);

export function logPath(file, env = process.env) {
  if (!files.has(file)) throw new Error("Unknown log destination");
  const raw = env.DATAAGENT_HOME?.trim() || path.join(homedir(), ".dataagent");
  const home = raw === "~" || raw.startsWith("~/") ? path.join(homedir(), raw.slice(1)) : raw;
  return path.resolve(home, "runtime", "logs", `${file}.log`);
}

function clean(value, seen = new WeakSet()) {
  if (typeof value === "string") return redactLogText(value);
  if (typeof value === "bigint") return String(value);
  if (!value || typeof value !== "object") return value;
  if (seen.has(value)) return "[Circular]";
  seen.add(value);
  if (value instanceof Error) return clean({ name: value.name, message: value.message, stack: value.stack, cause: value.cause, ...value }, seen);
  if (Array.isArray(value)) return value.map(item => clean(item, seen));
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, isSecretName(key) ? "[REDACTED]" : clean(item, seen)]));
}

export function createLogger({ file = "backend", component = file, env = process.env, directory, maxBytes = 5_000_000, backups = 3 } = {}) {
  if (!files.has(file)) throw new Error("Unknown log destination");
  const destination = directory ? path.join(directory, `${file}.log`) : logPath(file, env);
  let warned = false;
  const write = (level, message, metadata = {}) => {
    try {
      registerLogSecrets(Object.entries(env).filter(([key]) => isSecretName(key)).map(([, value]) => value));
      const record = clean({ ...metadata, ...(metadata.message ? { errorMessage: metadata.message } : {}), timestamp: metadata.timestamp ?? new Date().toISOString(), level, component: metadata.component ?? component, message });
      const line = JSON.stringify(record) + "\n";
      // The owning launcher collects this process's output through this same writer.
      if (env.DATAAGENT_LOG_CAPTURE === file) { process.stderr.write(line); return; }
      const state = path.dirname(path.dirname(destination));
      if (path.basename(path.dirname(destination)) === "logs" && path.basename(state) === "runtime") {
        fs.mkdirSync(state, { recursive: true, mode: 0o700 });
        fs.chmodSync(state, 0o700);
      }
      fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
      fs.chmodSync(path.dirname(destination), 0o700);
      if (fs.existsSync(destination)) fs.chmodSync(destination, 0o600);
      if (fs.existsSync(destination) && fs.statSync(destination).size + Buffer.byteLength(line) > maxBytes) {
        fs.rmSync(`${destination}.${backups}`, { force: true });
        for (let i = backups - 1; i >= 1; i--) if (fs.existsSync(`${destination}.${i}`)) fs.renameSync(`${destination}.${i}`, `${destination}.${i + 1}`);
        fs.renameSync(destination, `${destination}.1`);
      }
      fs.appendFileSync(destination, line, { encoding: "utf8", mode: 0o600 });
    } catch {
      if (!warned) { warned = true; process.stderr.write(`DataAgent: unable to write ${file}.log\n`); }
    }
  };
  return {
    path: destination, write,
    debug: (message, metadata) => write("DEBUG", message, metadata),
    info: (message, metadata) => write("INFO", message, metadata),
    warn: (message, metadata) => write("WARN", message, metadata),
    error: (message, errorOrMetadata, metadata) => write("ERROR", message,
      errorOrMetadata instanceof Error ? { ...metadata, error: errorOrMetadata }
        : errorOrMetadata != null && typeof errorOrMetadata !== "object"
          ? { ...metadata, error: { message: String(errorOrMetadata) } }
          : { ...errorOrMetadata, ...metadata }),
  };
}

/** Buffer whole lines so credentials split across chunks are redacted before any output. */
export function captureOutput(logger, { stage = "run", level = "INFO", echo } = {}) {
  let pending = "";
  const emit = line => {
    if (!line) return;
    try {
      const record = JSON.parse(line);
      if (typeof record.message === "string" && typeof record.level === "string" && typeof record.component === "string") {
        const { message, level: severity, ...metadata } = record;
        logger.write(severity, message, metadata);
      } else logger.write(level, line, { stage });
    } catch { logger.write(level, line, { stage }); }
    if (echo) echo.write(redactLogText(line) + "\n");
  };
  return {
    append(chunk) { pending += String(chunk); let end; while ((end = pending.indexOf("\n")) >= 0) { emit(pending.slice(0, end)); pending = pending.slice(end + 1); } },
    flush() { emit(pending); pending = ""; },
  };
}

const consoleInstalled = Symbol.for("dataagent.consoleLogging");
export function installConsoleLogging(options) {
  if (globalThis[consoleInstalled]) return;
  globalThis[consoleInstalled] = true;
  const logger = createLogger(options);
  for (const [method, level] of [["log", "INFO"], ["info", "INFO"], ["warn", "WARN"], ["error", "ERROR"]]) {
    const original = console[method].bind(console);
    console[method] = (...args) => {
      const message = format(...args);
      logger.write(level, message, { stage: "run" });
      if (process.env.DATAAGENT_LOG_CAPTURE !== options.file) original(redactLogText(message));
    };
  }
}
