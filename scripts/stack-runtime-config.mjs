import { join } from "node:path";
import { httpOrigin, readServerListen } from "./server-listen.mjs";

function port(value, fallback, name) {
  const parsed = Number(value ?? fallback);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`${name} must be an integer between 1 and 65535`);
  }
  return String(parsed);
}

export function resolveStackRuntimeConfig(env = process.env, args = []) {
  const listen = readServerListen(env);
  const web = webListen(args);
  return {
    apiHost: listen.host,
    apiPort: listen.port,
    webHost: web.host,
    webPort: web.port
  };
}

function webListen(args) {
  const overrides = {};
  for (let index = 0; index < args.length; index += 1) {
    const flag = args[index];
    if (flag !== "--web-host" && flag !== "--web-port") continue;
    const value = args[index + 1];
    if (value == null || value.startsWith("-")) throw new Error(`${flag} requires a value`);
    index += 1;
    if (flag === "--web-host") overrides.host = value.trim();
    else overrides.port = value;
  }
  const host = overrides.host || "127.0.0.1";
  if (/\s/u.test(host)) throw new Error("--web-host must be a host name or address");
  return { host, port: port(overrides.port, 3000, "--web-port") };
}

export function pythonControlPlaneLaunch(root, env, reload = false) {
  return {
    command: "uv",
    args: [
      "run", "--project", join(root, "runtime/agentkit"), "dataagent", "serve",
      ...(reload ? ["--reload"] : []),
    ],
    env,
  };
}

export { httpOrigin };

export function webLaunch(config, env = {}) {
  return {
    command: "npm",
    args: [
      "--prefix", "apps/web", "run", "start", "--",
      "--hostname", config.webHost,
      "--port", config.webPort,
    ],
    env: {
      ...env,
      API_PROXY_TARGET: httpOrigin(config.apiHost, config.apiPort),
    },
  };
}

export function formatStackEndpoints(config, enabled) {
  const lines = ["DataFoundry endpoints:"];
  if (enabled.startWeb) lines.push(`  GUI: ${httpOrigin(config.webHost, config.webPort)}`);
  if (enabled.startApi) lines.push(`  API: ${httpOrigin(config.apiHost, config.apiPort)}`);
  if (enabled.tuiReady) lines.push("  TUI: npm run start:tui");
  return lines.join("\n");
}
