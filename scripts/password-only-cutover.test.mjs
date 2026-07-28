import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { loadPasswordAuthConfig } from "../apps/api/dist/auth/config.js";

const ROOT = fileURLToPath(new URL("..", import.meta.url));

const FORBIDDEN = [
  "DATAFOUNDRY_AUTH_MODE",
  "NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE",
  "X-Dev-Token",
  "dev-token",
  "DEFAULT_DEV_USER",
  "upsertDevUser",
  "getByDevToken",
  "DemoCopilotKitClient",
  "--demo",
];

const CODE_ROOTS = ["apps", "packages", "scripts"];
const CODE_EXTENSIONS = new Set([".ts", ".tsx", ".js", ".mjs", ".cjs", ".json"]);
const ENV_FILES = [".env.example", "apps/web/.env.example"];

const EXCLUDED_DIR_NAMES = new Set([
  "node_modules",
  "dist",
  ".next",
  "coverage",
  ".git",
  "storage",
  "superpowers",
]);

/** Paths allowed to mention cutover-forbidden strings (error messages / gate itself). */
function isAllowlistedSource(relativePath) {
  const normalized = relativePath.replaceAll("\\", "/");
  if (normalized === "scripts/password-only-cutover.test.mjs") return true;
  if (normalized === "scripts/lib/metadata-test-identity.test.mjs") return true;
  if (normalized === "scripts/auth-foundation.test.mjs") return true;
  if (normalized === "scripts/deploy/cli.test.mjs") return true;
  if (normalized === "scripts/deploy/config.test.mjs") return true;
  if (normalized === "scripts/deploy/config.mjs") return true;
  if (normalized === "apps/tui/src/no-bare-fetch.guard.test.ts") return true;
  if (normalized === "apps/api/src/auth/config.ts") return true;
  if (normalized === "apps/api/dist/auth/config.js") return true;
  if (normalized.startsWith("packages/metadata/src/index.ts")) return true;
  if (normalized.startsWith("packages/metadata/dist/index.js")) return true;
  if (normalized.startsWith("docs/")) return true;
  if (normalized === "README.md" || normalized === "README_zh.md") return true;
  return false;
}

function walkFiles(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    if (EXCLUDED_DIR_NAMES.has(entry)) continue;
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      walkFiles(full, out);
      continue;
    }
    const ext = entry.includes(".") ? `.${entry.split(".").pop()}` : "";
    if (CODE_EXTENSIONS.has(ext) || entry.endsWith(".yml") || entry.endsWith(".yaml")) {
      out.push(full);
    }
  }
  return out;
}

function extractFencedBlocks(markdown) {
  const blocks = [];
  const re = /```[^\n]*\n([\s\S]*?)```/g;
  let match;
  while ((match = re.exec(markdown))) {
    blocks.push(match[1]);
  }
  return blocks;
}

test("product code and scripts have no runnable development auth bypasses", () => {
  const violations = [];
  for (const rootName of CODE_ROOTS) {
    const root = join(ROOT, rootName);
    for (const full of walkFiles(root)) {
      const rel = relative(ROOT, full);
      if (isAllowlistedSource(rel)) continue;
      const source = readFileSync(full, "utf8");
      for (const token of FORBIDDEN) {
        if (source.includes(token)) {
          violations.push(`${rel}: ${token}`);
        }
      }
    }
  }
  for (const envFile of ENV_FILES) {
    const full = join(ROOT, envFile);
    const source = readFileSync(full, "utf8");
    for (const token of FORBIDDEN) {
      if (source.includes(token)) {
        violations.push(`${envFile}: ${token}`);
      }
    }
  }
  assert.deepEqual(violations, [], `Forbidden development auth tokens remain:\n${violations.join("\n")}`);
});

test("CI workflow does not set DATAFOUNDRY_AUTH_MODE", () => {
  const ci = readFileSync(join(ROOT, ".github/workflows/ci.yml"), "utf8");
  assert.equal(ci.includes("DATAFOUNDRY_AUTH_MODE"), false);
});

test("loadPasswordAuthConfig rejects removed auth modes and requires password settings", () => {
  assert.doesNotThrow(() =>
    loadPasswordAuthConfig({
      DATAFOUNDRY_AUTH_MODE: "password",
      AUTH_SESSION_SECRET: "x".repeat(32),
      AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000",
      AUTH_REGISTRATION_MODE: "open",
      AUTH_EMAIL_DELIVERY: "test",
    })
  );
  assert.throws(
    () =>
      loadPasswordAuthConfig({
        DATAFOUNDRY_AUTH_MODE: "dev",
        AUTH_SESSION_SECRET: "x".repeat(32),
        AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000",
        AUTH_REGISTRATION_MODE: "open",
        AUTH_EMAIL_DELIVERY: "test",
      }),
    /DATAFOUNDRY_AUTH_MODE/
  );
  assert.throws(
    () =>
      loadPasswordAuthConfig({
        AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000",
        AUTH_REGISTRATION_MODE: "open",
        AUTH_EMAIL_DELIVERY: "test",
      }),
    /AUTH_SESSION_SECRET/
  );
});

test("fresh metadata schema has no dev_token and no default user", () => {
  const root = mkdtempSync(join(tmpdir(), "password-only-schema-"));
  try {
    const store = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
    const columns = store.db.prepare("PRAGMA table_info(users)").all().map((row) => row.name);
    assert.ok(!columns.includes("dev_token"));
    assert.equal(store.users.list().length, 0);
    store.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

function walkMarkdownFiles(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    if (EXCLUDED_DIR_NAMES.has(entry)) continue;
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      walkMarkdownFiles(full, out);
      continue;
    }
    if (entry.endsWith(".md")) out.push(full);
  }
  return out;
}

test("markdown fenced blocks omit runnable DATAFOUNDRY_AUTH_MODE examples", () => {
  const violations = [];
  const files = [join(ROOT, "README.md"), join(ROOT, "README_zh.md")];
  walkMarkdownFiles(join(ROOT, "docs"), files);
  for (const full of files) {
    const rel = relative(ROOT, full);
    const source = readFileSync(full, "utf8");
    for (const block of extractFencedBlocks(source)) {
      if (block.includes("DATAFOUNDRY_AUTH_MODE") || block.includes("NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE")) {
        violations.push(rel);
        break;
      }
    }
  }
  assert.deepEqual(violations, [], `Runnable auth-mode examples remain in fenced docs:\n${violations.join("\n")}`);
});

const FORBIDDEN_DOC_PHRASES = [
  "switch local development users",
  "switch local dev users",
  "Local development identity",
  "Default local identity",
  "切换本地开发用户",
  "本地开发身份",
  "本地默认身份",
];

test("public docs do not describe removed development identity UX", () => {
  const violations = [];
  const files = [join(ROOT, "README.md"), join(ROOT, "README_zh.md")];
  walkMarkdownFiles(join(ROOT, "docs"), files);
  for (const full of files) {
    const rel = relative(ROOT, full);
    const source = readFileSync(full, "utf8");
    for (const phrase of FORBIDDEN_DOC_PHRASES) {
      if (source.includes(phrase)) {
        violations.push(`${rel}: ${phrase}`);
      }
    }
  }
  assert.deepEqual(
    violations,
    [],
    `Docs still describe removed development identity UX:\n${violations.join("\n")}`
  );
});
