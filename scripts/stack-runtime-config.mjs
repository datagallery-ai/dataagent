import { join } from "node:path";
import { readServerListen } from "./server-listen.mjs";

function port(value, fallback, name) {
  const parsed = Number(value ?? fallback);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`${name} must be an integer between 1 and 65535`);
  }
  return String(parsed);
}

export function resolveStackRuntimeConfig(env = process.env) {
  const listen = readServerListen(env);
  return {
    API_HOST: listen.host,
    API_PORT: listen.port,
    WEB_HOST: env.WEB_HOST?.trim() || "127.0.0.1",
    WEB_PORT: port(env.WEB_PORT, 3000, "WEB_PORT")
  };
}

export function pythonControlPlaneLaunch(root, env, reload = false) {
  const child = { ...env };
  delete child.API_HOST;
  delete child.API_PORT;
  return {
    command: "uv",
    args: ["run", "--project", join(root, "runtime/agentkit"), "dataagent", "serve"],
    env: {
      ...child,
      ...(reload ? { API_RELOAD: env.API_RELOAD || "1" } : {}),
    },
  };
}

export function webProcessEnvironment(config) {
  return { HOSTNAME: config.WEB_HOST, PORT: config.WEB_PORT };
}

export function formatStackEndpoints(config, enabled) {
  const lines = ["DataFoundry endpoints:"];
  if (enabled.startWeb) lines.push(`  Web: http://127.0.0.1:${config.WEB_PORT}`);
  if (enabled.startApi) lines.push(`  API: http://${config.API_HOST}:${config.API_PORT}`);
  return lines.join("\n");
}
