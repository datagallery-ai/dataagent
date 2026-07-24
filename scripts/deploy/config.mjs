import { randomBytes } from "node:crypto";
import { mkdir, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";

/** Minimal dotenv parser — avoids requiring a root `dotenv` dependency for deploy scripts. */
export function parseDeploymentEnvironment(sourceText = "") {
  const env = {};
  for (const rawLine of String(sourceText).split(/\r?\n/u)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const matched = /^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/u.exec(line);
    if (!matched) continue;
    let value = matched[2] ?? "";
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    env[matched[1]] = value;
  }
  return env;
}

const DEFAULTS = {
  WEB_HOST: "0.0.0.0",
  WEB_PORT: "3000",
  API_HOST: "127.0.0.1",
  API_PORT: "8787",
  DATAFOUNDRY_AUTH_MODE: "password",
  AUTH_EMAIL_DELIVERY: "test",
  AUTH_REGISTRATION_MODE: "open",
  AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000",
  DATALINK_ENABLED: "false",
  DATALINK_MCP_HOST: "127.0.0.1",
  DATALINK_MCP_PORT: "8080",
  DATALINK_API_HOST: "127.0.0.1",
  DATALINK_API_PORT: "8081",
  STORAGE_ROOT_DIR: "storage",
  METADATA_DB_PATH: "storage/metadata/workbench.sqlite"
};

const SECRET_KEYS = ["AUTH_SESSION_SECRET", "SECRET_MASTER_KEY"];
const PLACEHOLDER_SECRETS = new Set(["", "change-me", "replace-me"]);
const SENSITIVE_KEY_PATTERN = /KEY|SECRET|TOKEN|PASSWORD|COOKIE|AUTHORIZATION/i;
const SENSITIVE_JSON_KEY_PATTERN = /^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret|token|password|authorization|auth)$/i;

export function isPlaceholderSecret(value) {
  return value == null || PLACEHOLDER_SECRETS.has(String(value).trim());
}

export function isCompleteDeploymentConfig(env = {}) {
  return Boolean(
    String(env.WEB_PORT ?? "").trim() &&
      String(env.API_PORT ?? "").trim() &&
      String(env.AUTH_PUBLIC_BASE_URL ?? "").trim() &&
      String(env.DATALINK_ENABLED ?? "").trim() &&
      !isPlaceholderSecret(env.AUTH_SESSION_SECRET) &&
      !isPlaceholderSecret(env.SECRET_MASTER_KEY)
  );
}

function maskSecret(value = "") {
  return "*".repeat(Math.min(8, Math.max(4, String(value).length || 4)));
}

function assertNoNewlines(value, key) {
  if (String(value).includes("\n") || String(value).includes("\r")) {
    throw new Error(`${key} must not contain newline characters`);
  }
}

function upsertEnvText(sourceText, updates) {
  const lines = sourceText.length > 0 ? sourceText.replace(/\r\n/g, "\n").split("\n") : [];
  if (lines.length > 0 && lines.at(-1) === "") lines.pop();

  const seen = new Set();
  const next = lines.map((line) => {
    const match = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(line);
    if (!match) return line;
    const key = match[1];
    if (!(key in updates)) return line;
    seen.add(key);
    assertNoNewlines(updates[key], key);
    return `${key}=${updates[key]}`;
  });

  for (const [key, value] of Object.entries(updates)) {
    if (seen.has(key)) continue;
    assertNoNewlines(value, key);
    next.push(`${key}=${value}`);
  }

  return `${next.join("\n")}\n`;
}

export function updateDeploymentEnvironment(sourceText, updates) {
  return upsertEnvText(sourceText ?? "", updates);
}

export function ensureDeploymentEnvironment(sourceText, options = {}) {
  const randomSecret = options.randomSecret ?? (() => randomBytes(32).toString("base64url"));
  const generateSecrets = options.generateSecrets !== false;
  const parsed = parseDeploymentEnvironment(sourceText ?? "");
  const updates = {};
  const generatedKeys = [];

  for (const [key, value] of Object.entries(DEFAULTS)) {
    if (parsed[key] == null || String(parsed[key]).trim() === "") {
      updates[key] = value;
      generatedKeys.push(key);
    }
  }

  for (const key of SECRET_KEYS) {
    if (isPlaceholderSecret(parsed[key])) {
      if (!generateSecrets) continue;
      updates[key] = randomSecret();
      generatedKeys.push(key);
    }
  }

  if (
    (parsed.AUTH_PUBLIC_BASE_URL == null || String(parsed.AUTH_PUBLIC_BASE_URL).trim() === "") &&
    updates.AUTH_PUBLIC_BASE_URL == null
  ) {
    const webPort = updates.WEB_PORT ?? parsed.WEB_PORT ?? DEFAULTS.WEB_PORT;
    updates.AUTH_PUBLIC_BASE_URL = `http://127.0.0.1:${webPort}`;
    generatedKeys.push("AUTH_PUBLIC_BASE_URL");
  }

  const text = Object.keys(updates).length > 0
    ? updateDeploymentEnvironment(sourceText ?? "", updates)
    : sourceText?.endsWith("\n") || sourceText === ""
      ? sourceText ?? ""
      : `${sourceText}\n`;

  const env = { ...parseDeploymentEnvironment(text) };
  return { text, env, generatedKeys };
}

export function renderWebEnvironment(env) {
  const authMode = env.DATAFOUNDRY_AUTH_MODE?.trim() || "password";
  const apiHost = env.API_HOST?.trim() || "127.0.0.1";
  const apiPort = env.API_PORT?.trim() || "8787";
  return [
    `NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=${authMode}`,
    "NEXT_PUBLIC_AGENT_RUNTIME_URL=",
    "NEXT_PUBLIC_CONFIG_API_URL=",
    `API_PROXY_TARGET=http://${apiHost}:${apiPort}`,
    ""
  ].join("\n");
}

async function writeAtomic(filePath, content, mode = 0o600) {
  await mkdir(path.dirname(filePath), { recursive: true });
  const tempPath = path.join(
    path.dirname(filePath),
    `.${path.basename(filePath)}.${process.pid}.${Date.now()}.tmp`
  );
  try {
    await writeFile(tempPath, content, { encoding: "utf8", mode });
    await rename(tempPath, filePath);
  } catch (error) {
    await rm(tempPath, { force: true }).catch(() => {});
    throw error;
  }
}

export async function writeDeploymentConfiguration(root, rootText, webText, options = {}) {
  const envPath = path.join(root, ".env");
  const webPath = path.join(root, "apps/web/.env.local");
  let backupPath;

  if (options.backup) {
    const stamp = options.timestamp ?? new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
    backupPath = path.join(root, `.env.backup-${stamp}`);
    const { readFile } = await import("node:fs/promises");
    let existing = "";
    try {
      existing = await readFile(envPath, "utf8");
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    await writeAtomic(backupPath, existing, 0o600);
  }

  await writeAtomic(envPath, rootText, 0o600);
  await writeAtomic(webPath, webText, 0o600);
  return { envPath, webPath, backupPath };
}

export function redactSensitiveText(text) {
  let result = String(text ?? "");

  result = result.replace(/^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/gm, (full, key, value) => {
    if (!SENSITIVE_KEY_PATTERN.test(key)) return full;
    return `${key}=${maskSecret(value)}`;
  });

  result = result.replace(
    /("([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*")([^"]*)(")/g,
    (full, prefix, key, value, suffix) => {
      if (!SENSITIVE_KEY_PATTERN.test(key) && !SENSITIVE_JSON_KEY_PATTERN.test(key)) return full;
      return `${prefix}${maskSecret(value)}${suffix}`;
    }
  );

  result = result.replace(
    /(Authorization:\s*Bearer\s+)(\S+)/gi,
    (_, prefix) => `${prefix}${maskSecret("bearer-token")}`
  );

  result = result.replace(
    /(https?:\/\/)([^/\s:@]+):([^/\s@]+)@/gi,
    (_, protocol) => `${protocol}****:****@`
  );

  result = result.replace(
    /\b((?:sk|rk|pk|tok)-[A-Za-z0-9_-]{8,}|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})\b/g,
    (value) => maskSecret(value)
  );

  return result;
}
