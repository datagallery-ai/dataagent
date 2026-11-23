import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";

/** Same Home as the backend: DATAAGENT_HOME, or ~/.dataagent. Relative values use the process directory. */
export function resolveDataagentHome(env: NodeJS.ProcessEnv = process.env): string {
  const configured = env.DATAAGENT_HOME?.trim();
  const raw = configured ? configured : join(homedir(), ".dataagent");
  const expanded = raw === "~" || raw.startsWith("~/")
    ? join(homedir(), raw.slice(raw === "~" ? 1 : 2))
    : raw;
  if (isAbsolute(expanded)) return expanded;
  return resolve(process.cwd(), expanded);
}

/** Interface log sits next to backend.log. There is no separate log-path setting. */
export function resolveTuiLogPath(env: NodeJS.ProcessEnv = process.env): string {
  return join(resolveDataagentHome(env), "runtime", "logs", "tui.log");
}
