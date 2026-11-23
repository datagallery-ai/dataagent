import assert from "node:assert/strict";
import test from "node:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));

const FORBIDDEN = [
  "X-Dev-Token",
  "dev-token",
  "DEFAULT_DEV_USER",
  "upsertDevUser",
  "getByDevToken",
  "DemoCopilotKitClient",
  "--demo",
];

const CODE_ROOTS = ["ui", "scripts"];
const CODE_EXTENSIONS = new Set([".ts", ".tsx", ".js", ".mjs", ".cjs", ".json"]);
const ENV_FILES = [".env.example"];

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
  if (normalized === "ui/tui/src/no-bare-fetch.guard.test.ts") return true;
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
