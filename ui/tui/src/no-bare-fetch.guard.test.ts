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
    if (["node_modules", "dist", ".next", ".git"].includes(entry)) {
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
  it("keeps production modules on injected fetch", () => {
    const offenders: string[] = [];
    const files = walk(srcRoot, (name) => /\.(ts|tsx)$/.test(name));
    assert.ok(files.length > 0, `expected source files under ${srcRoot}`);

    for (const file of files) {
      const rel = relative(srcRoot, file).replace(/\\/g, "/");
      if (rel.endsWith(".test.ts") || rel.endsWith(".test.tsx")) {
        continue;
      }
      const source = readFileSync(file, "utf8");
      if (/\bawait\s+fetch\s*\(/.test(source) || /\bfetch\s*\(\s*[`'"][^`'"]*\/api\//.test(source)) {
        offenders.push(rel);
      }
      if (/\bglobalThis\.fetch\s*\(/.test(source)) {
        // Defaults in constructors are OK; bare invocation is not.
        if (/globalThis\.fetch\s*\([^)]*\/api\//.test(source)) {
          offenders.push(rel);
        }
      }
    }

    assert.deepEqual(offenders, []);
  });

  it("forbids offline demo symbols in production sources", () => {
    const banned = new RegExp(
      ["DemoCopilotKitClient", "seedDemoState", ["-", "-", "demo"].join("")].join("|"),
    );
    const offenders: string[] = [];
    const uiRoot = join(repoRoot, "ui");
    const isCode = (name: string) => /\.(ts|tsx|js|mjs|cjs)$/.test(name);
    for (const dir of ["web/src", "tui/src", "shared"]) {
      const root = join(uiRoot, dir);
      const productionFiles = walk(root, (name) => isCode(name) && !/\.(test|d)\./.test(name));
      assert.ok(productionFiles.length > 0, `expected production source files under ${root}`);
    }
    const files = [
      ...walk(uiRoot, (name) => isCode(name) || /\.mdx?$/.test(name)),
      ...walk(join(repoRoot, "docs"), (name) => /\.mdx?$/.test(name)),
      join(repoRoot, "README.md"),
    ];
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
