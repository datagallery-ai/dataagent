import Busboy from "busboy";
import type { IncomingMessage } from "node:http";
import * as yaml from "js-yaml";
import yauzl, { type Entry, type ZipFile } from "yauzl";

const parseYaml = yaml.load;

const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;
const MAX_SKILL_MD_BYTES = 256 * 1024;
const MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024;
const MAX_ZIP_ENTRIES = 100;

/**
 * Busboy exposes multipart filenames as binary→Latin-1 strings. When the client
 * sent UTF-8 (typical for Chinese names), rebuild the original UTF-8 text.
 */
export const decodeMultipartFilename = (filename: string): string => {
  if (!filename) {
    return filename;
  }
  const asUtf8 = Buffer.from(filename, "latin1").toString("utf8");
  if (asUtf8 === filename || asUtf8.includes("\uFFFD")) {
    return filename;
  }
  if (Buffer.from(asUtf8, "utf8").toString("latin1") === filename) {
    return asUtf8;
  }
  return filename;
};

export type UploadedFile = {
  content: Buffer;
  filename: string;
  mimeType: string;
};

export type ParsedSkillUpload = {
  allowedTools: string[];
  description: string;
  instructions: string;
  manifest: {
    entry: string;
    files: string[];
    sizeBytes: number;
  };
  name: string;
  packageBase64: string;
  packageFileName: string;
  packageFormat: "skill-md" | "zip";
  version: string;
};

/** Parse one bounded multipart upload with a single file field. */
export const readMultipartUpload = (request: IncomingMessage): Promise<{ fields: Record<string, string>; file: UploadedFile }> =>
  new Promise((resolve, reject) => {
    const fields: Record<string, string> = {};
    let uploaded: UploadedFile | undefined;
    let failed = false;
    const parser = Busboy({
      headers: request.headers,
      limits: { fileSize: MAX_UPLOAD_BYTES, files: 1, fields: 20, fieldSize: 64 * 1024 }
    });
    parser.on("field", (name, value) => {
      fields[name] = value;
    });
    parser.on("file", (_name, stream, info) => {
      const chunks: Buffer[] = [];
      stream.on("limit", () => {
        failed = true;
        reject(new Error("UPLOAD_FILE_TOO_LARGE"));
      });
      stream.on("data", (chunk: Buffer) => chunks.push(chunk));
      stream.on("end", () => {
        if (!failed) {
          uploaded = {
            content: Buffer.concat(chunks),
            filename: decodeMultipartFilename(info.filename),
            mimeType: info.mimeType
          };
        }
      });
    });
    parser.on("error", reject);
    parser.on("finish", () => {
      if (failed) {
        return;
      }
      if (!uploaded) {
        reject(new Error("UPLOAD_FILE_REQUIRED"));
        return;
      }
      resolve({ fields, file: uploaded });
    });
    request.pipe(parser);
  });

/** Parse one bounded multipart request with multiple file fields. */
export const readMultipartFiles = (
  request: IncomingMessage,
  options: { maxFiles?: number; maxFileBytes?: number; maxTotalBytes?: number } = {}
): Promise<{ fields: Record<string, string>; files: UploadedFile[] }> =>
  new Promise((resolve, reject) => {
    const fields: Record<string, string> = {};
    const files: UploadedFile[] = [];
    let failed = false;
    let totalBytes = 0;
    const maxFiles = options.maxFiles ?? 20;
    const maxFileBytes = options.maxFileBytes ?? MAX_UPLOAD_BYTES;
    const maxTotalBytes = options.maxTotalBytes ?? maxFileBytes * maxFiles;
    const parser = Busboy({
      headers: request.headers,
      limits: { fileSize: maxFileBytes, files: maxFiles, fields: 50, fieldSize: 64 * 1024 }
    });
    parser.on("field", (name, value) => {
      fields[name] = value;
    });
    parser.on("filesLimit", () => {
      failed = true;
      reject(new Error("UPLOAD_FILE_COUNT_EXCEEDED"));
    });
    parser.on("file", (_name, stream, info) => {
      const chunks: Buffer[] = [];
      let size = 0;
      stream.on("limit", () => {
        failed = true;
        reject(new Error("UPLOAD_FILE_TOO_LARGE"));
      });
      stream.on("data", (chunk: Buffer) => {
        size += chunk.length;
        totalBytes += chunk.length;
        if (totalBytes > maxTotalBytes && !failed) {
          failed = true;
          reject(new Error("UPLOAD_TOTAL_TOO_LARGE"));
          stream.resume();
          return;
        }
        chunks.push(chunk);
      });
      stream.on("end", () => {
        if (!failed && size > 0) {
          files.push({
            content: Buffer.concat(chunks),
            filename: decodeMultipartFilename(info.filename),
            mimeType: info.mimeType
          });
        }
      });
    });
    parser.on("error", reject);
    parser.on("finish", () => {
      if (failed) {
        return;
      }
      if (files.length === 0) {
        reject(new Error("UPLOAD_FILE_REQUIRED"));
        return;
      }
      resolve({ fields, files });
    });
    request.pipe(parser);
  });

/** Validate and parse a SKILL.md or zip skill package without extracting it to disk. */
export const parseSkillUpload = async (file: UploadedFile): Promise<ParsedSkillUpload> => {
  const lowerName = file.filename.toLowerCase();
  if (lowerName.endsWith(".md")) {
    if (file.content.length > MAX_SKILL_MD_BYTES) {
      throw new Error("SKILL_MD_TOO_LARGE");
    }
    return buildSkillPackage(file.content.toString("utf8"), file, "skill-md", [file.filename], file.content.length);
  }
  if (!lowerName.endsWith(".zip")) {
    throw new Error("SKILL_PACKAGE_TYPE_UNSUPPORTED");
  }
  const entries = await readSafeZip(file.content);
  const skillEntries = entries.filter((entry) => entry.name === "SKILL.md" || entry.name.endsWith("/SKILL.md"));
  if (skillEntries.length !== 1) {
    throw new Error("SKILL_MD_REQUIRED_ONCE");
  }
  const skill = skillEntries[0];
  if (!skill || skill.content.length > MAX_SKILL_MD_BYTES) {
    throw new Error("SKILL_MD_TOO_LARGE");
  }
  return buildSkillPackage(
    skill.content.toString("utf8"),
    file,
    "zip",
    entries.map((entry) => entry.name),
    entries.reduce((sum, entry) => sum + entry.content.length, 0),
    skill.name
  );
};

const buildSkillPackage = (
  content: string,
  file: UploadedFile,
  packageFormat: "skill-md" | "zip",
  files: string[],
  sizeBytes: number,
  entry = "SKILL.md"
): ParsedSkillUpload => {
  const match = /^---\s*\r?\n([\s\S]*?)\r?\n---\s*\r?\n([\s\S]*)$/u.exec(content.trim());
  if (!match) {
    throw new Error("SKILL_FRONTMATTER_REQUIRED");
  }
  const frontmatter = parseFrontmatter(match[1] ?? "");
  const instructions = (match[2] ?? "").trim();
  const name = stringValue(frontmatter.name);
  const description = stringValue(frontmatter.description);
  if (!name || !description) {
    throw new Error("SKILL_NAME_DESCRIPTION_REQUIRED");
  }
  const allowed = frontmatter["allowed-tools"] ?? frontmatter.allowedTools;
  return {
    allowedTools: stringList(allowed),
    description,
    instructions,
    manifest: { entry, files, sizeBytes },
    name,
    packageBase64: file.content.toString("base64"),
    packageFileName: file.filename,
    packageFormat,
    version: stringValue(frontmatter.version) ?? "1.0.0"
  };
};

const readSafeZip = (buffer: Buffer): Promise<Array<{ content: Buffer; name: string }>> =>
  new Promise((resolve, reject) => {
    yauzl.fromBuffer(buffer, { lazyEntries: true, validateEntrySizes: true }, (openError, zipFile) => {
      if (openError || !zipFile) {
        reject(openError ?? new Error("SKILL_ZIP_OPEN_FAILED"));
        return;
      }
      collectZipEntries(zipFile).then(resolve, reject);
    });
  });

const collectZipEntries = (zipFile: ZipFile): Promise<Array<{ content: Buffer; name: string }>> =>
  new Promise((resolve, reject) => {
    const files: Array<{ content: Buffer; name: string }> = [];
    let totalBytes = 0;
    let entryCount = 0;
    const fail = (error: unknown): void => {
      zipFile.close();
      reject(error);
    };
    zipFile.on("entry", (entry: Entry) => {
      entryCount += 1;
      if (entryCount > MAX_ZIP_ENTRIES || !isSafeZipPath(entry.fileName) || isSymlink(entry)) {
        fail(new Error("SKILL_ZIP_ENTRY_UNSAFE"));
        return;
      }
      if (entry.fileName.endsWith("/")) {
        zipFile.readEntry();
        return;
      }
      totalBytes += entry.uncompressedSize;
      if (totalBytes > MAX_UNCOMPRESSED_BYTES) {
        fail(new Error("SKILL_ZIP_EXPANDED_TOO_LARGE"));
        return;
      }
      zipFile.openReadStream(entry, (streamError, stream) => {
        if (streamError || !stream) {
          fail(streamError ?? new Error("SKILL_ZIP_ENTRY_READ_FAILED"));
          return;
        }
        const chunks: Buffer[] = [];
        stream.on("data", (chunk: Buffer) => chunks.push(chunk));
        stream.on("error", fail);
        stream.on("end", () => {
          files.push({ content: Buffer.concat(chunks), name: entry.fileName });
          zipFile.readEntry();
        });
      });
    });
    zipFile.on("end", () => resolve(files));
    zipFile.on("error", reject);
    zipFile.readEntry();
  });

const isSafeZipPath = (value: string): boolean => {
  const normalized = value.replaceAll("\\", "/");
  return !normalized.startsWith("/")
    && !/^[a-zA-Z]:/u.test(normalized)
    && !normalized.split("/").includes("..")
    && !normalized.includes("\0");
};

const isSymlink = (entry: Entry): boolean => ((entry.externalFileAttributes >>> 16) & 0o170000) === 0o120000;

const parseFrontmatter = (value: string): Record<string, unknown> => {
  const parsed: unknown = parseYaml(value);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("SKILL_FRONTMATTER_INVALID");
  }
  return parsed as Record<string, unknown>;
};

const stringValue = (value: unknown): string | undefined =>
  typeof value === "string" && value.trim() ? value.trim() : undefined;

const stringList = (value: unknown): string[] => {
  if (Array.isArray(value)) {
    return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
      .map((item) => item.trim());
  }
  const text = stringValue(value);
  return text ? text.replace(/^\[|\]$/gu, "").split(",").map((item) => item.trim()).filter(Boolean) : [];
};
