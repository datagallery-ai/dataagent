import type { ArtifactType } from "@datafoundry/contracts";

const ALLOWED_EXTENSIONS = new Set([
  ".csv",
  ".htm",
  ".html",
  ".json",
  ".md",
  ".png",
  ".svg",
  ".tsv",
  ".txt",
  ".xlsx"
]);

const EXCLUDED_EXTENSIONS = new Set([
  ".cjs",
  ".js",
  ".mjs",
  ".py",
  ".sh",
  ".tmp",
  ".ts"
]);

const EXCLUDED_DIRECTORY_PREFIXES = ["scratch/", "tmp/"];

export const normalizeSessionOutputPath = (path: string): string =>
  path.replaceAll("\\", "/").replace(/^\.\/+/, "");

export const shouldIngestSessionOutputPath = (path: string): boolean => {
  const normalized = normalizeSessionOutputPath(path);
  if (!normalized || normalized.includes("\0")) {
    return false;
  }
  if (EXCLUDED_DIRECTORY_PREFIXES.some((prefix) => normalized === prefix.slice(0, -1) || normalized.startsWith(prefix))) {
    return false;
  }
  const baseName = normalized.slice(normalized.lastIndexOf("/") + 1);
  const dotIndex = baseName.lastIndexOf(".");
  if (dotIndex <= 0) {
    return false;
  }
  const extension = baseName.slice(dotIndex).toLowerCase();
  if (EXCLUDED_EXTENSIONS.has(extension)) {
    return false;
  }
  return ALLOWED_EXTENSIONS.has(extension);
};

export const inferOutputTypeFromPath = (path: string): ArtifactType => {
  const normalized = normalizeSessionOutputPath(path).toLowerCase();
  if (normalized.endsWith(".md")) {
    return "markdown";
  }
  if (normalized.endsWith(".html") || normalized.endsWith(".htm")) {
    return "html";
  }
  if (/\.(png|svg|jpe?g|gif|webp)$/.test(normalized)) {
    return "image";
  }
  return "file";
};
