import type { FileAssetService } from "@datafoundry/files";
import type { ConfigResourceRecord, MetadataStore } from "@datafoundry/metadata";
import * as yaml from "js-yaml";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { basename, dirname, extname, join, relative, resolve, sep } from "node:path";
import yauzl, { type Entry, type ZipFile } from "yauzl";

const parseYaml = yaml.load;

const MAX_SKILL_MD_BYTES = 256 * 1024;
const MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024;
const MAX_ZIP_ENTRIES = 100;

export type SkillPackageFormat = "skill-md" | "zip";

export type SkillMode = "all" | "auto" | "none" | "selected";

export type SkillToolMergeStrategy = "intersection" | "union";

export type UploadedSkillFile = {
  content: Buffer;
  filename: string;
  mimeType?: string;
};

export type ParsedSkillPackage = {
  allowedTools: string[];
  deniedTools: string[];
  description: string;
  instructions: string;
  manifest: {
    entry: string;
    files: string[];
    sizeBytes: number;
  };
  name: string;
  packageFileName: string;
  packageFormat: SkillPackageFormat;
  tags: string[];
  userInvocable: boolean;
  version: string;
};

export type SkillRecord = {
  allowedTools: string[];
  builtin: boolean;
  defaultDbIds: string[];
  defaultEnabled: boolean;
  defaultKbIds: string[];
  defaultMcpIds: string[];
  deniedTools: string[];
  description: string;
  id: string;
  modelProfileId?: string;
  name: string;
  packageEntry: string;
  packageFileRefId?: string;
  packageFiles: string[];
  packageFormat: SkillPackageFormat;
  revision: number;
  scope: "builtin" | "user" | "workspace";
  status: string;
  tags: string[];
  userInvocable: boolean;
  version: string;
};

export type SkillPolicyConfig = {
  allowedToolNames?: string[];
  deniedToolNames: string[];
  maxSkills: number;
  requireUserInvocable: boolean;
  strictSkillTools: boolean;
};

export type SkillRunConfig = {
  activeSkillId?: string;
  enabledSkillIds: string[];
  skillIds: string[];
  skillMode: SkillMode;
  skillPolicy: SkillPolicyConfig;
  skillTags: string[];
};

export type SkillSelectionAuditItem = {
  decision: "rejected" | "selected";
  reasons: string[];
  score?: number;
  skillId: string;
};

export type SkillSelectionResult = {
  audit: SkillSelectionAuditItem[];
  effectiveToolPolicy: {
    allowedTools?: string[];
    deniedTools: string[];
    mergeStrategy: SkillToolMergeStrategy;
  };
  selectedSkills: SkillRecord[];
};

export type SelectSkillsInput = {
  chatMode?: string;
  fileNames?: string[];
  metadataStore: MetadataStore;
  runConfig: SkillRunConfig;
  userId: string;
  userInput: string;
  workspaceId?: string;
};

export type MaterializedSkill = {
  id: string;
  name: string;
  path: string;
};

export type MaterializeSkillPackagesInput = {
  fileAssetService: FileAssetService;
  runDir: string;
  skills: SkillRecord[];
  userId: string;
  workspaceId?: string;
};

/** Validate and parse a SKILL.md or zip skill package without extracting it to disk. */
export const parseSkillPackage = async (file: UploadedSkillFile): Promise<ParsedSkillPackage> => {
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

/** Convert a persisted config resource into the normalized skill record used at runtime. */
export const configResourceToSkillRecord = (resource: ConfigResourceRecord): SkillRecord => {
  const payload = resource.payload;
  const packageFileRefId = stringValue(payload.packageFileRefId ?? payload.package_file_ref_id);
  const modelProfileId = stringValue(payload.modelProfileId ?? payload.model_profile_id);
  return {
    allowedTools: stringList(payload.allowedTools ?? payload.allowed_tools),
    builtin: resource.builtin,
    defaultDbIds: stringList(payload.defaultDbIds ?? payload.default_db_ids),
    defaultEnabled: resource.default_enabled,
    defaultKbIds: stringList(payload.defaultKbIds ?? payload.default_kb_ids),
    defaultMcpIds: stringList(payload.defaultMcpIds ?? payload.default_mcp_ids),
    deniedTools: stringList(payload.deniedTools ?? payload.denied_tools),
    description: resource.description ?? stringValue(payload.description) ?? "",
    id: resource.id,
    ...(modelProfileId ? { modelProfileId } : {}),
    name: stringValue(payload.name) ?? resource.name,
    packageEntry: stringValue(payload.packageEntry ?? payload.package_entry) ?? "SKILL.md",
    ...(packageFileRefId ? { packageFileRefId } : {}),
    packageFiles: stringList(payload.packageFiles ?? payload.package_files),
    packageFormat: parsePackageFormat(payload.packageFormat ?? payload.package_format),
    revision: resource.revision,
    scope: parseScope(payload.scope, resource.builtin),
    status: resource.status,
    tags: stringList(payload.tags),
    userInvocable: booleanValue(payload.userInvocable ?? payload.user_invocable, true),
    version: stringValue(payload.version) ?? "1.0.0"
  };
};

/** Build the persisted payload for one uploaded skill package. */
export const buildSkillResourcePayload = (input: {
  fields?: Record<string, string>;
  packageFileRefId: string;
  parsed: ParsedSkillPackage;
}): Record<string, unknown> => {
  const fields = input.fields ?? {};
  const scope = parseScope(fields.scope, false);
  const extraTags = stringList(fields.tags);
  const modelProfileId = stringValue(fields.modelProfileId ?? fields.model_profile_id);
  const packageSource = stringValue(fields.packageSource ?? fields.package_source);
  return {
    allowedTools: input.parsed.allowedTools,
    defaultDbIds: stringList(fields.defaultDbIds ?? fields.default_db_ids),
    defaultKbIds: stringList(fields.defaultKbIds ?? fields.default_kb_ids),
    defaultMcpIds: stringList(fields.defaultMcpIds ?? fields.default_mcp_ids),
    deniedTools: input.parsed.deniedTools,
    description: input.parsed.description,
    manifest: input.parsed.manifest,
    name: input.parsed.name,
    packageEntry: input.parsed.manifest.entry,
    packageFileName: input.parsed.packageFileName,
    packageFileRefId: input.packageFileRefId,
    packageFiles: input.parsed.manifest.files,
    packageFormat: input.parsed.packageFormat,
    ...(packageSource ? { packageSource } : {}),
    scope,
    tags: unique([...input.parsed.tags, ...extraTags]),
    userInvocable: input.parsed.userInvocable,
    version: input.parsed.version,
    ...(modelProfileId ? { modelProfileId } : {})
  };
};

/** Select the exact skill set that one run is allowed to expose through Mastra workspace skills. */
export const selectSkillsForRun = (input: SelectSkillsInput): SkillSelectionResult => {
  const workspaceId = input.workspaceId ?? "default";
  const policy = input.runConfig.skillPolicy;
  const candidates = input.metadataStore.configResources.list({
    workspace_id: workspaceId,
    user_id: input.userId,
    kind: "skill"
  }).map(configResourceToSkillRecord);
  const explicitIds = unique([
    ...input.runConfig.skillIds,
    ...input.runConfig.enabledSkillIds,
    ...(input.runConfig.activeSkillId ? [input.runConfig.activeSkillId] : [])
  ]);
  const audit: SkillSelectionAuditItem[] = [];

  if (input.runConfig.skillMode === "none") {
    candidates.forEach((skill) => audit.push({ decision: "rejected", reasons: ["mode:none"], skillId: skill.id }));
    return {
      audit,
      effectiveToolPolicy: { deniedTools: policy.deniedToolNames, mergeStrategy: "union" },
      selectedSkills: []
    };
  }

  const ranked = candidates
    .map((skill) => scoreSkill(skill, input, explicitIds))
    .filter((entry) => {
      if (entry.rejected) {
        audit.push({ decision: "rejected", reasons: entry.reasons, skillId: entry.skill.id });
        return false;
      }
      return true;
    })
    .sort((left, right) => right.score - left.score || left.skill.name.localeCompare(right.skill.name));

  // Prefer the user-selected active skill when maxSkills truncates the ranked list.
  const activeSkillId = input.runConfig.activeSkillId;
  const selected = (() => {
    if (!activeSkillId || policy.maxSkills <= 0) {
      return ranked.slice(0, policy.maxSkills);
    }
    const activeEntry = ranked.find((entry) => entry.skill.id === activeSkillId);
    if (!activeEntry) {
      return ranked.slice(0, policy.maxSkills);
    }
    const rest = ranked.filter((entry) => entry.skill.id !== activeSkillId);
    return [activeEntry, ...rest.slice(0, Math.max(0, policy.maxSkills - 1))];
  })();

  selected.forEach((entry) => {
    audit.push({
      decision: "selected",
      reasons: entry.reasons,
      score: entry.score,
      skillId: entry.skill.id
    });
  });

  return {
    audit,
    effectiveToolPolicy: buildEffectiveToolPolicy(selected.map((entry) => entry.skill), policy),
    selectedSkills: selected.map((entry) => entry.skill)
  };
};

/** Materialize selected skill packages into the run workspace and return Mastra skill paths. */
export const materializeSkillPackages = async (input: MaterializeSkillPackagesInput): Promise<MaterializedSkill[]> => {
  const workspaceId = input.workspaceId ?? "default";
  const skillsRoot = resolve(input.runDir, "skills");
  mkdirSync(skillsRoot, { recursive: true });
  const materialized: MaterializedSkill[] = [];
  for (const skill of input.skills) {
    if (!skill.packageFileRefId) {
      throw new Error(`SKILL_PACKAGE_FILE_REF_REQUIRED:${skill.id}`);
    }
    const skillDirName = safePathSegment(skill.name || skill.id);
    const skillDir = resolve(skillsRoot, skillDirName);
    assertChildPath(skillsRoot, skillDir);
    rmSync(skillDir, { force: true, recursive: true });
    mkdirSync(skillDir, { recursive: true });
    const packageFile = input.fileAssetService.readRef({
      id: skill.packageFileRefId,
      user_id: input.userId,
      workspace_id: workspaceId
    });
    if (skill.packageFormat === "skill-md") {
      writeFileSync(resolve(skillDir, "SKILL.md"), packageFile.body);
    } else {
      await extractZipToSkillDir(packageFile.body, skillDir);
    }
    const relativePath = relative(input.runDir, skillDir).split(sep).join("/");
    materialized.push({ id: skill.id, name: skill.name, path: relativePath });
  }
  return materialized;
};

const buildSkillPackage = (
  content: string,
  file: UploadedSkillFile,
  packageFormat: SkillPackageFormat,
  files: string[],
  sizeBytes: number,
  entry = "SKILL.md"
): ParsedSkillPackage => {
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
  return {
    allowedTools: stringList(frontmatter["allowed-tools"] ?? frontmatter.allowedTools),
    deniedTools: stringList(frontmatter["denied-tools"] ?? frontmatter.deniedTools),
    description,
    instructions,
    manifest: { entry, files, sizeBytes },
    name,
    packageFileName: file.filename,
    packageFormat,
    tags: stringList(frontmatter.tags),
    userInvocable: booleanValue(frontmatter["user-invocable"] ?? frontmatter.userInvocable, true),
    version: stringValue(frontmatter.version) ?? "1.0.0"
  };
};

const scoreSkill = (
  skill: SkillRecord,
  input: SelectSkillsInput,
  explicitIds: string[]
): { rejected: boolean; reasons: string[]; score: number; skill: SkillRecord } => {
  const reasons: string[] = [];
  let score = 0;
  const policy = input.runConfig.skillPolicy;
  if (!skill.packageFileRefId) {
    return { rejected: true, reasons: ["package:missing-file-ref"], score, skill };
  }
  if (skill.status === "disabled" || skill.status === "archived") {
    return { rejected: true, reasons: [`status:${skill.status}`], score, skill };
  }
  if (policy.requireUserInvocable && !skill.userInvocable) {
    return { rejected: true, reasons: ["policy:not-user-invocable"], score, skill };
  }
  if (input.runConfig.skillMode === "selected" && !explicitIds.includes(skill.id)) {
    return { rejected: true, reasons: ["mode:selected:not-requested"], score, skill };
  }
  if (input.runConfig.skillMode === "all") {
    return { rejected: false, reasons: ["mode:all"], score: 1, skill };
  }
  if (explicitIds.includes(skill.id)) {
    reasons.push("explicit:id");
    score += 100;
  }
  if (skill.defaultEnabled) {
    reasons.push("workspace:default-enabled");
  }
  input.runConfig.skillTags.forEach((tag) => {
    if (skill.tags.includes(tag)) {
      reasons.push(`tag:${tag}`);
      score += 10;
    }
  });
  const queryTokens = tokenize(input.userInput);
  const searchable = tokenize([skill.name, skill.description, skill.tags.join(" ")].join(" "));
  queryTokens.forEach((token) => {
    if (searchable.includes(token)) {
      reasons.push(`query:${token}`);
      score += skill.name.toLowerCase().includes(token) ? 8 : 5;
    }
  });
  (input.fileNames ?? []).forEach((fileName) => {
    const extension = extname(fileName).replace(".", "").toLowerCase();
    if (extension && skill.tags.includes(extension)) {
      reasons.push(`file:${extension}`);
      score += 3;
    }
  });
  if (input.chatMode && skill.tags.includes(input.chatMode)) {
    reasons.push(`chat_mode:${input.chatMode}`);
    score += 3;
  }
  if (input.runConfig.skillMode === "auto" && score <= 0) {
    return { rejected: true, reasons: ["auto:no-match"], score, skill };
  }
  return { rejected: false, reasons: reasons.length > 0 ? reasons : ["auto:candidate"], score, skill };
};

const buildEffectiveToolPolicy = (
  skills: SkillRecord[],
  policy: SkillPolicyConfig
): SkillSelectionResult["effectiveToolPolicy"] => {
  const declaredAllowed = skills.map((skill) => skill.allowedTools).filter((tools) => tools.length > 0);
  const skillAllowed = declaredAllowed.length === 0
    ? undefined
    : policy.strictSkillTools
      ? declaredAllowed.reduce((left, right) => left.filter((tool) => right.includes(tool)))
      : unique(declaredAllowed.flat());
  const allowedTools = policy.allowedToolNames && skillAllowed
    ? policy.allowedToolNames.filter((tool) => skillAllowed.includes(tool))
    : policy.allowedToolNames ?? skillAllowed;
  return {
    ...(allowedTools && allowedTools.length > 0 ? { allowedTools } : {}),
    deniedTools: unique([...policy.deniedToolNames, ...skills.flatMap((skill) => skill.deniedTools)]),
    mergeStrategy: policy.strictSkillTools ? "intersection" : "union"
  };
};

const readSafeZip = (buffer: Buffer): Promise<Array<{ content: Buffer; name: string }>> =>
  new Promise((resolvePromise, reject) => {
    yauzl.fromBuffer(buffer, { lazyEntries: true, validateEntrySizes: true }, (openError, zipFile) => {
      if (openError || !zipFile) {
        reject(openError ?? new Error("SKILL_ZIP_OPEN_FAILED"));
        return;
      }
      collectZipEntries(zipFile).then(resolvePromise, reject);
    });
  });

const collectZipEntries = (zipFile: ZipFile): Promise<Array<{ content: Buffer; name: string }>> =>
  new Promise((resolvePromise, reject) => {
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
    zipFile.on("error", fail);
    zipFile.on("end", () => resolvePromise(files));
    zipFile.readEntry();
  });

const extractZipToSkillDir = async (buffer: Buffer, targetDir: string): Promise<void> => {
  const entries = await readSafeZip(buffer);
  const skillEntry = entries.find((entry) => entry.name === "SKILL.md" || entry.name.endsWith("/SKILL.md"));
  if (!skillEntry) {
    throw new Error("SKILL_MD_REQUIRED_ONCE");
  }
  const rootPrefix = skillEntry.name.endsWith("/SKILL.md")
    ? skillEntry.name.slice(0, -"SKILL.md".length)
    : "";
  entries.forEach((entry) => {
    if (rootPrefix && !entry.name.startsWith(rootPrefix)) {
      return;
    }
    const relativeName = rootPrefix && entry.name.startsWith(rootPrefix)
      ? entry.name.slice(rootPrefix.length)
      : entry.name;
    if (!relativeName) {
      return;
    }
    const targetPath = resolve(targetDir, relativeName);
    assertChildPath(targetDir, targetPath);
    mkdirSync(dirname(targetPath), { recursive: true });
    writeFileSync(targetPath, entry.content);
  });
};

const parseFrontmatter = (value: string): Record<string, unknown> => {
  const parsed: unknown = parseYaml(value);
  return isRecord(parsed) ? parsed : {};
};

const isSafeZipPath = (value: string): boolean =>
  Boolean(value)
  && !value.startsWith("/")
  && !value.includes("\\")
  && !value.split("/").some((part) => part === ".." || part === ".");

const isSymlink = (entry: Entry): boolean => ((entry.externalFileAttributes >>> 16) & 0o170000) === 0o120000;

const assertChildPath = (root: string, path: string): void => {
  if (path !== root && !path.startsWith(`${root}${sep}`)) {
    throw new Error("SKILL_PATH_ESCAPE");
  }
};

const safePathSegment = (value: string): string =>
  basename(value).toLowerCase().replace(/[^a-z0-9._-]+/gu, "-").replace(/^-|-$/gu, "") || "skill";

const tokenize = (value: string): string[] =>
  unique(value.toLowerCase().split(/[^a-z0-9_\u4e00-\u9fa5]+/u).filter((token) => token.length >= 2));

const parsePackageFormat = (value: unknown): SkillPackageFormat => value === "zip" ? "zip" : "skill-md";

const parseScope = (value: unknown, builtin: boolean): SkillRecord["scope"] => {
  if (builtin) {
    return "builtin";
  }
  return value === "user" ? "user" : "workspace";
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
const stringValue = (value: unknown): string | undefined =>
  typeof value === "string" && value.trim() ? value.trim() : undefined;
const booleanValue = (value: unknown, fallback: boolean): boolean => typeof value === "boolean" ? value : fallback;
const stringList = (value: unknown): string[] => {
  if (Array.isArray(value)) {
    return unique(value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
      .map((item) => item.trim()));
  }
  if (typeof value === "string") {
    return unique(value.split(",").map((item) => item.trim()).filter(Boolean));
  }
  return [];
};
const unique = <T>(values: T[]): T[] => [...new Set(values)];
