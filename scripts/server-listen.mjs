import { randomUUID } from 'node:crypto';
import { effectiveEnvironment } from './lib/config-env.mjs';
import { checkYamlPath, checkLegacy, readConfigYaml, parseConfigYaml, configurationExists } from './lib/config-yaml.mjs';
import { mkdirSync, writeFileSync, renameSync, rmSync, openSync, closeSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HOME_CONFIG_TEMPLATE = fileURLToPath(
  new URL("../runtime/agentkit/dataagent/bootstrap/templates/config.yaml", import.meta.url),
);

export const DEFAULT_SERVER_HOST = "127.0.0.1";
export const DEFAULT_SERVER_PORT = "8790";

export function httpOrigin(host, port) {
  const bare = String(host).trim().replace(/^\[(.*)\]$/u, "$1");
  const shown = bare.includes(":") ? `[${bare}]` : bare;
  return `http://${shown}:${port}`;
}

/**
 * Home, same rule as the backend (`home_path`): a blank DATAAGENT_HOME means unset
 * and gives ~/.dataagent; `~` expands; a relative value uses the process directory.
 */
export function resolveHome(env = process.env) {
  const raw = env.DATAAGENT_HOME?.trim() || join(homedir(), ".dataagent");
  const expanded = raw === "~" || raw.startsWith("~/")
    ? join(homedir(), raw.slice(raw === "~" ? 1 : 2))
    : raw;
  return isAbsolute(expanded) ? expanded : resolve(process.cwd(), expanded);
}

export function readServerListen(env = process.env, configPath, envFile) {
  const home = resolveHome(env);
  const file = configPath?.trim() || join(home, 'config.yaml');
  checkYamlPath(file);
  checkLegacy(file);
  if (!configPath && !configurationExists(file)) return { host: DEFAULT_SERVER_HOST, port: DEFAULT_SERVER_PORT };
  const { value } = readConfigYaml(file);
  const server = Object.hasOwn(value, "server") ? value.server : {};
  if (!server || typeof server !== 'object' || Array.isArray(server)) throw new Error(`${file}: invalid server mapping`);
  const environment = effectiveEnvironment(env, home, envFile);
  function expand(value) {
    return typeof value === 'string' ? value.replace(/\$env\{([A-Za-z_][A-Za-z0-9_]*)\}/gu, (_, name) => {
      if (!environment[name]) throw new Error(`${file}: missing server environment variable`);
      return environment[name];
    }) : value;
  }
  const rawHost = expand(Object.hasOwn(server, "host") ? server.host : DEFAULT_SERVER_HOST);
  const rawPort = expand(Object.hasOwn(server, "port") ? server.port : DEFAULT_SERVER_PORT);
  const port = Number(rawPort);
  if (typeof rawHost !== 'string' || !rawHost.trim() || (/[\s/]/u.test(rawHost.trim()))) throw new Error(`${file}: invalid server host`);
  if (typeof rawPort === 'boolean' || !Number.isInteger(port) || port < 1 || port > 65535) throw new Error(`${file}: invalid server port`);
  return { host: rawHost.trim().replace(/^\[(.*)\]$/u, '$1'), port: String(port) };
}

/** Edit only server fields and atomically replace with a private same-directory file. */
export function writeServerListen(env, listen) {
  const file = join(resolveHome(env), 'config.yaml');
  checkLegacy(file);
  const { doc } = readConfigYaml(configurationExists(file) ? file : HOME_CONFIG_TEMPLATE);
  doc.setIn(['server', 'host'], listen.host);
  doc.setIn(['server', 'port'], Number(listen.port));
  const text = doc.toString();
  parseConfigYaml(text, file);
  mkdirSync(dirname(file), { recursive: true });
  const temporary = `${file}.${randomUUID()}.tmp`;
  let created = false;
  try {
    const descriptor = openSync(temporary, 'wx', 0o600);
    created = true;
    try { writeFileSync(descriptor, text); }
    finally { closeSync(descriptor); }
    renameSync(temporary, file);
  } finally {
    if (created) rmSync(temporary, { force: true });
  }
}
