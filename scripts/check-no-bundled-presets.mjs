#!/usr/bin/env node
// scripts/check-no-bundled-presets.mjs
//
// CI grep gate 鈥?fail the build if the source tree ships bundled persona
// presets for trademarked / copyrighted characters.
//
// Why this exists: a user-facing persona / pet / avatar feature is the kind of
// surface where a maintainer (or a contributor copy-pasting from somewhere) can
// accidentally drop in a known IP 鈥?Vocaloid, Genshin, Re:Zero, Arknights 鈥?and
// ship it under the project's own name. By the time a takedown arrives the
// diff has already been merged. This gate scans source-only paths and fails
// the build on a hit, so the maintainer sees it in the same PR.
//
// Add a name to BLOCKED_PATTERNS when (a) it's a known copyrighted character,
// (b) the user community could mistake a built-in preset for the official IP,
// or (c) we have no permission to redistribute the persona text. To allowlist
// a hit, rename the source file to one of EXCLUDED_FILE_NAMES or extend that
// set with a comment.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = fileURLToPath(new URL(".", import.meta.url));
const REPO_ROOT = resolve(SCRIPT_DIR, "..");

const BLOCKED_PATTERNS = [
  // Vocaloid brand family
  /vocaloid/i,
  /hatsune/i,
  /miku/i,
  // Kagamine twins (handled together since one is rarely used without the other)
  /kagamine/i,
  // Re:Zero
  /\banya\b/i,
  /re:zero/i,
  // Touhou
  /\btouhou\b/i,
  // HoYoverse
  /\bgenshin\b/i,
  // Arknights
  /arknights/i,
  // Blue Archive
  /blue.?archive/i,
  // Star Rail
  /star.?rail/i,
];

// Scan source roots. Missing roots are silently skipped so the same script
// works whether or not a given surface (desktop app, mobile shell) exists.
const SEARCH_ROOTS = [
  "apps",
  "packages",
];

const EXCLUDED_DIR_NAMES = new Set([
  "node_modules",
  "dist",
  "build",
  ".git",
  "__tests__",
  "tests",
  "fixtures",
  "test-data",
  "test_data",
]);

const SCAN_EXTENSIONS = new Set([
  ".ts", ".tsx", ".mjs", ".js", ".cjs", ".json",
]);

const EXCLUDED_FILE_NAMES = new Set([
  "check-no-bundled-presets.mjs",
]);

/** Walk `root` recursively; yield `path` of each matching file. */
function* walkSource(rootAbs) {
  const stack = [rootAbs];
  while (stack.length > 0) {
    const current = stack.pop();
    let entries;
    try {
      entries = readdirSync(current, { withFileTypes: true });
    } catch {
      continue;
    }
    for (const entry of entries) {
      const full = join(current, entry.name);
      if (entry.isDirectory()) {
        if (EXCLUDED_DIR_NAMES.has(entry.name)) continue;
        stack.push(full);
      } else if (entry.isFile()) {
        const dot = entry.name.lastIndexOf(".");
        const ext = dot >= 0 ? entry.name.slice(dot) : "";
        if (!SCAN_EXTENSIONS.has(ext)) continue;
        if (EXCLUDED_FILE_NAMES.has(entry.name)) continue;
        yield full;
      }
    }
  }
}

const matches = [];

for (const root of SEARCH_ROOTS) {
  const rootAbs = join(REPO_ROOT, root);
  try {
    statSync(rootAbs);
  } catch {
    continue; // missing root 鈥?silently skip
  }
  for (const file of walkSource(rootAbs)) {
    let body;
    try {
      body = readFileSync(file, "utf8");
    } catch {
      continue;
    }
    for (const pattern of BLOCKED_PATTERNS) {
      const re = new RegExp(pattern.source, pattern.flags.includes("g") ? pattern.flags : pattern.flags + "g");
      let m;
      while ((m = re.exec(body)) !== null) {
        const before = body.lastIndexOf("\n", m.index) + 1;
        const after = body.indexOf("\n", m.index);
        const lineEnd = after === -1 ? body.length : after;
        const lineStart = before === 0 ? 0 : before;
        const line = body.slice(lineStart, lineEnd);
        const lineNumber = body.slice(0, m.index).split("\n").length;
        matches.push({
          file: relative(REPO_ROOT, file).split(sep).join("/"),
          line: lineNumber,
          match: m[0],
          lineText: line.trim().slice(0, 160),
        });
        if (m.index === re.lastIndex) re.lastIndex++;
      }
    }
  }
}

if (matches.length > 0) {
  console.error("[check-no-bundled-presets] FAIL 鈥?bundled preset keyword(s) found:");
  for (const m of matches) {
    console.error(`  ${m.file}:${m.line}  [${m.match}]  ${m.lineText}`);
  }
  console.error("");
  console.error("Bundled presets for trademarked / copyrighted characters are forbidden.");
  console.error("To allowlist a hit, add the file name to EXCLUDED_FILE_NAMES or");
  console.error("remove the keyword from the source.");
  process.exit(1);
}

console.log("[check-no-bundled-presets] OK 鈥?no bundled preset keywords in source tree");
process.exit(0);
