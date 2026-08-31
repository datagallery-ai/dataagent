import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, normalize, relative } from "node:path";

const roots = ["README.md", "README_zh.md", "CONTRIBUTING.md", "docs", "apps/web/README.md", "apps/tui/README.md"].filter(existsSync);
const files = roots.flatMap((root) => collectDocFiles(root)).sort();

const brokenLinks = [];
const forbiddenPublicLinks = [];
const sensitiveMatches = [];
const markdownLinkPattern = /\[[^\]]*]\(([^)]+)\)/g;

for (const file of files) {
  const text = readFileSync(file, "utf8");
  scanLinks(file, text);
  scanSensitiveContent(file, text);
}

if (brokenLinks.length > 0) {
  console.error("Documentation link smoke failed:");
  for (const link of brokenLinks) {
    console.error(`- ${link.file}: ${link.rawTarget} -> ${link.resolvedPath}`);
  }
}

if (forbiddenPublicLinks.length > 0) {
  console.error("Public documentation must not link to .docs-internal:");
  for (const link of forbiddenPublicLinks) {
    console.error(`- ${link.file}: ${link.rawTarget}`);
  }
}

if (sensitiveMatches.length > 0) {
  console.error("Documentation sensitive-content smoke failed:");
  for (const match of sensitiveMatches) {
    console.error(`- ${match.file}: ${match.label} at line ${match.line}`);
  }
}

if (brokenLinks.length > 0 || forbiddenPublicLinks.length > 0 || sensitiveMatches.length > 0) {
  process.exit(1);
}

console.log(`Documentation link smoke OK: files=${files.length}`);

function collectDocFiles(path) {
  if (!existsSync(path)) {
    return [];
  }
  const stat = statSync(path);
  if (stat.isFile()) {
    return isDocFile(path) ? [path] : [];
  }
  if (!stat.isDirectory()) {
    return [];
  }
  return readdirSync(path)
    .flatMap((entry) => collectDocFiles(join(path, entry)));
}

function isDocFile(path) {
  return path.endsWith(".md") || path.endsWith(".html");
}

function scanLinks(file, text) {
  let match;
  while ((match = markdownLinkPattern.exec(text)) !== null) {
    const rawTarget = match[1]?.trim() ?? "";
    if (shouldSkip(rawTarget)) {
      continue;
    }
    const targetPath = rawTarget.split("#")[0] ?? "";
    if (!targetPath) {
      continue;
    }
    const resolvedPath = normalize(join(dirname(file), targetPath));
    if (isPublicDoc(file) && pointsToInternalDocs(resolvedPath)) {
      forbiddenPublicLinks.push({ file, rawTarget });
    }
    if (!existsSync(resolvedPath)) {
      brokenLinks.push({ file, rawTarget, resolvedPath });
    }
  }
}

function scanSensitiveContent(file, text) {
  const checks = [
    { label: "source-sensitive product reference", pattern: /\bDB-GPT(?:-like)?\b/i },
    { label: "source-sensitive reference repo", pattern: /\bReference repo\b/i },
    { label: "source-sensitive wording", pattern: /借鉴|对标|复刻/ },
    { label: "personal Unix path", pattern: /\/home\/[A-Za-z0-9._-]+|\/Users\/[A-Za-z0-9._-]+/ },
    { label: "personal Windows path", pattern: /C:\\Users\\/i },
    { label: "OpenAI-style secret key", pattern: /\bsk-[A-Za-z0-9_-]{20,}\b/ },
    { label: "AWS access key", pattern: /\bAKIA[0-9A-Z]{16}\b/ },
    {
      label: "literal credential assignment",
      pattern: /\b(?:API_KEY|TOKEN|SECRET|PASSWORD|PASSWD)\s*=\s*(?!replace-with-your-key|你的_API_Key|<[^>]+>|\.\.\.)\S+/i
    }
  ];
  const publicOnlyChecks = [
    { label: "public release placeholder wording", pattern: /待确认|未确认|未核实|占位|后续补充|可能支持/ }
  ];
  const activeChecks = isPublicDoc(file) ? [...checks, ...publicOnlyChecks] : checks;

  const lines = text.split("\n");
  for (const [index, line] of lines.entries()) {
    for (const check of activeChecks) {
      if (check.pattern.test(line)) {
        sensitiveMatches.push({ file, label: check.label, line: index + 1 });
      }
    }
  }
}

function isPublicDoc(file) {
  return file === "README.md" || file === "README_zh.md" || file.startsWith(`docs${"/"}`);
}

function pointsToInternalDocs(path) {
  return relative(".docs-internal", path) === "" || !relative(".docs-internal", path).startsWith("..");
}

function shouldSkip(rawTarget) {
  return (
    !rawTarget ||
    rawTarget.startsWith("#") ||
    rawTarget.startsWith("http:") ||
    rawTarget.startsWith("https:") ||
    rawTarget.startsWith("mailto:") ||
    rawTarget.startsWith("app://")
  );
}
