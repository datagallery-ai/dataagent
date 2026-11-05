import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HOME_CONFIG_TEMPLATE = fileURLToPath(
  new URL("../runtime/agentkit/dataagent/bootstrap/templates/config.json", import.meta.url),
);

export const DEFAULT_SERVER_HOST = "127.0.0.1";
export const DEFAULT_SERVER_PORT = "8790";

export function httpOrigin(host, port) {
  const bare = String(host).trim().replace(/^\[(.*)\]$/u, "$1");
  const shown = bare.includes(":") ? `[${bare}]` : bare;
  return `http://${shown}:${port}`;
}

function homeDir(env) {
  if (Object.prototype.hasOwnProperty.call(env, "DATAAGENT_HOME")) {
    return env.DATAAGENT_HOME?.trim() || null;
  }
  return join(homedir(), ".dataagent");
}

function readServerSection(file) {
  if (!file || !existsSync(file)) return {};
  try {
    const server = JSON.parse(readFileSync(file, "utf8"))?.server;
    return server && typeof server === "object" ? server : {};
  } catch {
    return {};
  }
}

function pickHost(value, fallback) {
  if (typeof value !== "string") return fallback;
  const host = value.trim().replace(/^\[(.*)\]$/u, "$1");
  return host || fallback;
}

function pickPort(value, fallback) {
  const port = Number(value);
  if (Number.isInteger(port) && port >= 1 && port <= 65535) return String(port);
  return fallback;
}

function listenFromServer(server) {
  return {
    host: pickHost(server.host, DEFAULT_SERVER_HOST),
    port: pickPort(server.port, DEFAULT_SERVER_PORT),
  };
}

function readExplicitListen(file) {
  if (!existsSync(file)) throw new Error(`Configuration file does not exist: ${file}`);
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(file, "utf8"));
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new Error(`Configuration file is not valid JSON (${file}): ${reason}`);
  }
  if (!isPlainObject(parsed)) throw new Error(`Configuration file is not a JSON object: ${file}`);
  return listenFromServer(isPlainObject(parsed.server) ? parsed.server : {});
}

/** Listen address from `--config` when set, otherwise Home config.json. */
export function readServerListen(env = process.env, configPath) {
  const explicit = typeof configPath === "string" ? configPath.trim() : "";
  if (explicit) return readExplicitListen(explicit);
  const home = homeDir(env);
  const base = home ? readServerSection(join(home, "config.json")) : {};
  return listenFromServer(base);
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

/** Full Home template. A server-only file would make initialize_home skip this copy. */
function freshHomeConfig() {
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(HOME_CONFIG_TEMPLATE, "utf8"));
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    throw new Error(`Cannot read the Home config template ${HOME_CONFIG_TEMPLATE}: ${reason}`);
  }
  if (!isPlainObject(parsed)) {
    throw new Error(`Home config template is not a JSON object: ${HOME_CONFIG_TEMPLATE}`);
  }
  return parsed;
}

/**
 * Persist the wizard's listen choice into Home config.json.
 * A missing file is created from the full default template, then server is updated.
 * An existing object only changes server.host and server.port.
 * Unreadable JSON is left untouched.
 */
export function writeServerListen(env, listen) {
  const home = env.DATAAGENT_HOME?.trim();
  if (!home) throw new Error("DATAAGENT_HOME is required to write server.host and server.port");
  const file = join(home, "config.json");
  mkdirSync(dirname(file), { recursive: true });
  let doc;
  if (!existsSync(file)) {
    doc = freshHomeConfig();
  } else {
    const original = readFileSync(file, "utf8");
    let parsed;
    try {
      parsed = JSON.parse(original);
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      throw new Error(`Home config.json is not valid JSON and was left unchanged (${file}): ${reason}`);
    }
    if (!isPlainObject(parsed)) {
      throw new Error(`Home config.json is not a JSON object and was left unchanged: ${file}`);
    }
    doc = parsed;
  }
  const server = isPlainObject(doc.server) ? doc.server : {};
  doc.server = { ...server, host: listen.host, port: Number(listen.port) };
  writeFileSync(file, `${JSON.stringify(doc, null, 2)}\n`);
}
