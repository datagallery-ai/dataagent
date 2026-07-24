import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, dirname, join, relative } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const moduleDir = dirname(fileURLToPath(import.meta.url));
const packageRoot = basename(moduleDir) === "dist" || basename(moduleDir) === "src"
  ? join(moduleDir, "..")
  : moduleDir;
const srcRoot = join(packageRoot, "src");
const repoRoot = join(packageRoot, "..", "..");

function walk(dir: string, predicate: (name: string) => boolean): string[] {
  if (!existsSync(dir)) {
    return [];
  }
  const entries = readdirSync(dir);
  const files: string[] = [];
  for (const entry of entries) {
    if (entry === "node_modules" || entry === "dist" || entry === ".git") {
      continue;
    }
    const full = join(dir, entry);
    const info = statSync(full);
    if (info.isDirectory()) {
      files.push(...walk(full, predicate));
    } else if (predicate(entry)) {
      files.push(full);
    }
  }
  return files;
}

describe("no bare fetch on protected TUI API paths", () => {
  it("keeps production modules on injected fetch / AuthenticatedTransport", () => {
    const allowed = new Set([
      "auth/authenticated-transport.ts",
      "auth/auth-client.ts",
    ]);
    const offenders: string[] = [];
    const files = walk(srcRoot, (name) => /\.(ts|tsx)$/.test(name));
    assert.ok(files.length > 0, `expected source files under ${srcRoot}`);

    for (const file of files) {
      const rel = relative(srcRoot, file).replace(/\\/g, "/");
      if (rel.endsWith(".test.ts") || rel.endsWith(".test.tsx")) {
        continue;
      }
      if (allowed.has(rel)) {
        continue;
      }
      const source = readFileSync(file, "utf8");
      if (/\bawait\s+fetch\s*\(/.test(source) || /\bfetch\s*\(\s*[`'"][^`'"]*\/api\//.test(source)) {
        offenders.push(rel);
      }
      if (/\bglobalThis\.fetch\s*\(/.test(source) && !rel.includes("auth/")) {
        // Defaults in constructors are OK; bare invocation is not.
        if (/globalThis\.fetch\s*\([^)]*\/api\//.test(source)) {
          offenders.push(rel);
        }
      }
    }

    assert.deepEqual(offenders, []);
  });

  it("forbids offline demo symbols in apps, README, and docs", () => {
    const banned = /DemoCopilotKitClient|seedDemoState|--demo/;
    const offenders: string[] = [];
    const roots = [
      join(repoRoot, "apps"),
      join(repoRoot, "docs"),
      join(repoRoot, "README.md"),
    ];

    const files: string[] = [];
    for (const root of roots) {
      if (!existsSync(root)) {
        continue;
      }
      const info = statSync(root);
      if (info.isFile()) {
        files.push(root);
        continue;
      }
      files.push(
        ...walk(root, (name) => /\.(ts|tsx|js|mjs|cjs|md|mdx)$/.test(name)),
      );
    }
    assert.ok(files.length > 0, "expected apps/docs/README files to scan");

    for (const file of files) {
      const rel = relative(repoRoot, file).replace(/\\/g, "/");
      // Guard + intentional CLI rejection string-split must keep mentioning the flag.
      if (
        rel.endsWith("no-bare-fetch.guard.test.ts")
        || rel.endsWith("no-bare-fetch.guard.test.js")
      ) {
        continue;
      }
      const source = readFileSync(file, "utf8");
      if (banned.test(source)) {
        offenders.push(rel);
      }
    }

    assert.deepEqual(offenders, []);
  });
});
