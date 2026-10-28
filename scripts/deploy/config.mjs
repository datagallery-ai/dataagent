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

const PLACEHOLDER_SECRETS = new Set(["", "change-me", "replace-me"]);
const SENSITIVE_KEY_PATTERN = /KEY|SECRET|TOKEN|PASSWORD|COOKIE|AUTHORIZATION/i;
const SENSITIVE_JSON_KEY_PATTERN = /^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|secret|token|password|authorization|auth)$/i;

export function isPlaceholderSecret(value) {
  return value == null || PLACEHOLDER_SECRETS.has(String(value).trim());
}

export function deploymentConfigGaps() {
  return [];
}

export function isCompleteDeploymentConfig(env = {}) {
  return deploymentConfigGaps(env).length === 0;
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

export function ensureDeploymentEnvironment(sourceText = "") {
  const text = sourceText.endsWith("\n") || sourceText === "" ? sourceText : `${sourceText}\n`;
  return { text, env: parseDeploymentEnvironment(text), generatedKeys: [] };
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

export async function writeDeploymentConfiguration(root, rootText, options = {}) {
  const envPath = path.join(root, ".env");
  let backupPath;
  const { readFile } = await import("node:fs/promises");

  let existing = "";
  try {
    existing = await readFile(envPath, "utf8");
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }

  const existingEnv = parseDeploymentEnvironment(existing);
  const hasExistingSecrets = Object.entries(existingEnv).some(([key, value]) =>
    SENSITIVE_KEY_PATTERN.test(key) && !isPlaceholderSecret(value)
  );
  const shouldBackup = Boolean(options.backup) || Boolean(options.backupExistingSecrets && hasExistingSecrets);

  if (shouldBackup) {
    const stamp = options.timestamp ?? new Date().toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
    backupPath = path.join(root, `.env.backup-${stamp}`);
    await writeAtomic(backupPath, existing, 0o600);
  }

  await writeAtomic(envPath, rootText, 0o600);
  return { envPath, backupPath };
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
