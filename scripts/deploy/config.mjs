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
  WEB_HOST: "127.0.0.1",
  WEB_PORT: "3000",
  API_HOST: "127.0.0.1",
  API_PORT: "8787",
  AUTH_EMAIL_DELIVERY: "test",
  AUTH_REGISTRATION_MODE: "open",
  STORAGE_ROOT_DIR: "storage",
  METADATA_DB_PATH: "storage/metadata/workbench.sqlite"
};

const SECRET_KEYS = ["AUTH_SESSION_SECRET", "SECRET_MASTER_KEY"];
/** Removed by password-only cutover; strip on ensure so upgraded .env can boot. */
const REMOVED_LEGACY_AUTH_KEYS = ["DATAFOUNDRY_AUTH_MODE", "NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE"];
const PLACEHOLDER_SECRETS = new Set(["", "change-me", "replace-me"]);
const SENSITIVE_KEY_PATTERN = /KEY|SECRET|TOKEN|PASSWORD|COOKIE|AUTHORIZATION/i;
const SENSITIVE_JSON_KEY_PATTERN = /^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret|token|password|authorization|auth)$/i;

export function removeEnvKeys(sourceText, keys) {
  const keySet = new Set(keys);
  const lines = String(sourceText ?? "").length > 0
    ? String(sourceText).replace(/\r\n/g, "\n").split("\n")
    : [];
  if (lines.length > 0 && lines.at(-1) === "") lines.pop();
  const removedKeys = [];
  const next = [];
  for (const line of lines) {
    const match = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(line);
    if (match && keySet.has(match[1])) {
      if (!removedKeys.includes(match[1])) removedKeys.push(match[1]);
      continue;
    }
    next.push(line);
  }
  return {
    text: next.length > 0 ? `${next.join("\n")}\n` : "",
    removedKeys
  };
}

export function isPlaceholderSecret(value) {
  return value == null || PLACEHOLDER_SECRETS.has(String(value).trim());
}

export function isCompleteDeploymentConfig(env = {}) {
  const registrationMode = String(env.AUTH_REGISTRATION_MODE ?? "").trim();
  return Boolean(
    String(env.WEB_PORT ?? "").trim() &&
      String(env.API_PORT ?? "").trim() &&
      String(env.AUTH_PUBLIC_BASE_URL ?? "").trim() &&
      (registrationMode === "open" || registrationMode === "closed") &&
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
  const stripped = removeEnvKeys(sourceText ?? "", REMOVED_LEGACY_AUTH_KEYS);
  const workingText = stripped.text;
  const removedKeys = stripped.removedKeys;
  const parsed = parseDeploymentEnvironment(workingText);
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

  if (parsed.AUTH_PUBLIC_BASE_URL == null || String(parsed.AUTH_PUBLIC_BASE_URL).trim() === "") {
    const webPort = updates.WEB_PORT ?? parsed.WEB_PORT ?? DEFAULTS.WEB_PORT;
    updates.AUTH_PUBLIC_BASE_URL = `http://127.0.0.1:${webPort}`;
    if (!generatedKeys.includes("AUTH_PUBLIC_BASE_URL")) {
      generatedKeys.push("AUTH_PUBLIC_BASE_URL");
    }
  }

  const text = Object.keys(updates).length > 0
    ? updateDeploymentEnvironment(workingText, updates)
    : workingText?.endsWith("\n") || workingText === ""
      ? workingText
      : `${workingText}\n`;

  const env = { ...parseDeploymentEnvironment(text) };
  delete env.DATAFOUNDRY_AUTH_MODE;
  delete env.NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE;
  return { text, env, generatedKeys, removedKeys };
}

export function renderWebEnvironment(env) {
  const apiHost = env.API_HOST?.trim() || "127.0.0.1";
  const apiPort = env.API_PORT?.trim() || "8787";
  return [
    "NEXT_PUBLIC_AGENT_RUNTIME_URL=",
    "NEXT_PUBLIC_CONFIG_API_URL=",
    `API_PROXY_TARGET=http://${apiHost}:${apiPort}`,
    ""
  ].join("\n");
}

function isLoopbackHost(hostname) {
  const host = String(hostname ?? "")
    .toLowerCase()
    .replace(/^\[(.*)\]$/u, "$1");
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

/**
 * Native deploy allows HTTP only on loopback. Non-loopback hosts must use HTTPS
 * (or SSH port forwarding to a loopback listener).
 */
export function assertNativeAuthPublicBaseUrl(raw) {
  let url;
  try {
    url = new URL(String(raw ?? "").trim());
  } catch {
    throw new Error("AUTH_PUBLIC_BASE_URL must be a valid absolute URL");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("AUTH_PUBLIC_BASE_URL must use http or https");
  }
  if (url.protocol === "http:" && !isLoopbackHost(url.hostname)) {
    throw new Error(
      "AUTH_PUBLIC_BASE_URL HTTP is only allowed for loopback hosts (127.0.0.1, localhost, ::1). " +
        "For remote access use SSH port forwarding to the loopback listener, or configure HTTPS."
    );
  }
  return url;
}

export function assertNativeBindHosts(env = {}) {
  for (const key of ["WEB_HOST", "API_HOST"]) {
    const host = String(env[key] ?? "").trim();
    if (!host) continue;
    if (host === "0.0.0.0" || host === "::") {
      throw new Error(
        `${key}=${host} exposes the service on all interfaces over plain HTTP. ` +
          "Native password-only installs bind loopback by default; use SSH forwarding or a TLS reverse proxy."
      );
    }
  }
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
  const { readFile } = await import("node:fs/promises");

  let existing = "";
  try {
    existing = await readFile(envPath, "utf8");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  const existingEnv = parseDeploymentEnvironment(existing);
  const hasExistingSecrets = SECRET_KEYS.some((key) => !isPlaceholderSecret(existingEnv[key]));
  const shouldBackup = Boolean(options.backup) || Boolean(options.backupExistingSecrets && hasExistingSecrets);

  if (shouldBackup) {
    const stamp = options.timestamp ?? new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
    backupPath = path.join(root, `.env.backup-${stamp}`);
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
    /([a-z][a-z0-9+.-]*:\/\/)([^/\s:@]+):([^/\s@]+)@/gi,
    (_, protocol) => `${protocol}****:****@`
  );
  result = result.replace(
    /\b((?:sk|rk|pk|tok)-[A-Za-z0-9_-]{8,}|(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})\b/g,
    (value) => maskSecret(value)
  );

  return result;
}
