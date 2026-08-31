import type {
  FileAssetRecord,
  FileAssetRefRecord,
  FileAssetRefSource,
  MetadataStore
} from "@datafoundry/metadata";
import { createHash, randomUUID } from "node:crypto";
import { copyFileSync, linkSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve, sep } from "node:path";

export type CreateFileAssetRefInput = {
  user_id: string;
  workspace_id: string;
  filename: string;
  content: Buffer;
  declared_mime_type?: string;
  source: FileAssetRefSource;
  session_id?: string;
  run_id?: string;
  metadata?: unknown;
};

export type CreateFileAssetRefFromPathInput = Omit<CreateFileAssetRefInput, "content"> & {
  path: string;
};

export type ResolvedFileAssetRef = {
  asset: FileAssetRecord;
  ref: FileAssetRefRecord;
};

export type FileAssetService = {
  createRef(input: CreateFileAssetRefInput): ResolvedFileAssetRef;
  createRefFromPath(input: CreateFileAssetRefFromPathInput): ResolvedFileAssetRef;
  deleteRef(input: { user_id: string; workspace_id: string; id: string }): FileAssetRefRecord;
  getRef(input: { user_id: string; workspace_id: string; id: string }): ResolvedFileAssetRef;
  listRefs(input: {
    user_id: string;
    workspace_id: string;
    limit?: number;
    source?: FileAssetRefSource;
    /**
     * Session filter. `null` → only cross-session refs (session_id IS NULL);
     * a string → only that session's refs; omitted → no session filter.
     */
    session_id?: string | null;
    /** When true, match only refs that HAVE a session_id (scope=session w/o an id). */
    has_session?: boolean;
  }): ResolvedFileAssetRef[];
  materializeRefToPath(input: {
    ref: FileAssetRefRecord;
    targetPath: string;
    linkStrategy?: "copy" | "hardlink";
  }): void;
  readRef(input: { user_id: string; workspace_id: string; id: string }): { body: Buffer; mimeType: string };
  /**
   * Sync a workspace file into the asset store, keyed by filename.
   * - No existing ref for this filename → create one (source: "workspace").
   * - Existing ref, same sha256 → no-op (content unchanged).
   * - Existing ref, different sha256 → reassign the ref to the new asset (file_id stays
   *   stable); the previous asset is left orphaned for gcOrphanAssets.
   * Returns the resolved ref.
   */
  syncWorkspaceFile(input: {
    user_id: string;
    workspace_id: string;
    filename: string;
    path: string;
    declared_mime_type?: string;
    session_id?: string;
    run_id?: string;
  }): ResolvedFileAssetRef;
  /**
   * Promote a file-type artifact into a cross-session workspace asset (R-022).
   * Creates or reuses a `source="workspace"` ref with `session_id IS NULL` pointing at
   * the same file_asset_id as the source ref — no byte copy (asset store is content-
   * addressed and deduped). Idempotent by filename: an existing cross-session workspace
   * ref with the same filename is reassigned to the source asset (file_id stable) or
   * returned as-is if it already points at it.
   */
  /**
   * Promote a session-scoped file ref into a cross-session workspace asset (R-022 / file
   * promote). Creates or reuses a `source="workspace"` ref with `session_id IS NULL`
   * pointing at the same file_asset_id as the source ref — no byte copy (asset store is
   * content-addressed and deduped). Idempotent by filename: an existing cross-session
   * workspace ref with the same filename is reassigned to the source asset (file_id
   * stable) or returned as-is if it already points at it.
   */
  promoteFileToWorkspace(input: {
    user_id: string;
    workspace_id: string;
    /** The session-scoped file_asset_ref_id to promote. */
    file_asset_ref_id: string;
    filename?: string;
    declared_mime_type?: string;
  }): ResolvedFileAssetRef;
  /** Delete on-disk content and records for assets with zero non-deleted refs. */
  gcOrphanAssets(): { removed: number };
};

export type LocalFileAssetServiceOptions = {
  storageRoot?: string;
};

export class LocalFileAssetService implements FileAssetService {
  private readonly root: string;

  constructor(
    private readonly metadataStore: MetadataStore,
    options: LocalFileAssetServiceOptions = {}
  ) {
    this.root = resolve(options.storageRoot ?? process.env.FILE_ASSET_STORAGE_ROOT ?? "storage/files");
  }

  createRef(input: CreateFileAssetRefInput): ResolvedFileAssetRef {
    const sha256 = hashBuffer(input.content);
    const storagePath = this.assetStoragePath(sha256);
    const existing = this.metadataStore.fileAssets.findBySha256(sha256);
    if (!existing) {
      mkdirSync(dirname(storagePath), { recursive: true });
      const temporaryPath = `${storagePath}.${randomUUID()}.tmp`;
      writeFileSync(temporaryPath, input.content);
      renameSync(temporaryPath, storagePath);
    }
    const asset = this.metadataStore.fileAssets.create({
      id: existing?.id ?? randomUUID(),
      sha256,
      size_bytes: input.content.length,
      storage_path: storagePath,
      ...(input.declared_mime_type ? { detected_mime_type: input.declared_mime_type } : {})
    });
    const ref = this.metadataStore.fileAssetRefs.create({
      id: randomUUID(),
      file_asset_id: asset.id,
      user_id: input.user_id,
      workspace_id: input.workspace_id,
      filename: safeFilename(input.filename),
      ...(input.declared_mime_type ? { declared_mime_type: input.declared_mime_type } : {}),
      source: input.source,
      ...(input.session_id ? { session_id: input.session_id } : {}),
      ...(input.run_id ? { run_id: input.run_id } : {}),
      ...(input.metadata !== undefined ? { metadata_json: input.metadata } : {})
    });
    return { asset, ref };
  }

  createRefFromPath(input: CreateFileAssetRefFromPathInput): ResolvedFileAssetRef {
    return this.createRef({
      ...input,
      content: readFileSync(input.path)
    });
  }

  deleteRef(input: { user_id: string; workspace_id: string; id: string }): FileAssetRefRecord {
    return this.metadataStore.fileAssetRefs.softDelete(input);
  }

  getRef(input: { user_id: string; workspace_id: string; id: string }): ResolvedFileAssetRef {
    const ref = this.metadataStore.fileAssetRefs.get(input);
    const asset = this.metadataStore.fileAssets.get({ id: ref.file_asset_id });
    this.assertStoragePath(asset.storage_path);
    return { asset, ref };
  }

  listRefs(input: {
    user_id: string;
    workspace_id: string;
    limit?: number;
    source?: FileAssetRefSource;
    session_id?: string | null;
    has_session?: boolean;
  }): ResolvedFileAssetRef[] {
    return this.metadataStore.fileAssetRefs.list(input).map((ref) => ({
      asset: this.metadataStore.fileAssets.get({ id: ref.file_asset_id }),
      ref
    }));
  }

  materializeRefToPath(input: {
    ref: FileAssetRefRecord;
    targetPath: string;
    linkStrategy?: "copy" | "hardlink";
  }): void {
    const asset = this.metadataStore.fileAssets.get({ id: input.ref.file_asset_id });
    this.assertStoragePath(asset.storage_path);
    mkdirSync(dirname(input.targetPath), { recursive: true });
    if ((input.linkStrategy ?? "hardlink") === "hardlink") {
      try {
        linkSync(asset.storage_path, input.targetPath);
        return;
      } catch {
        // Cross-device filesystems and some sandboxed volumes do not support hard links.
      }
    }
    copyFileSync(asset.storage_path, input.targetPath);
  }

  readRef(input: { user_id: string; workspace_id: string; id: string }): { body: Buffer; mimeType: string } {
    const { asset, ref } = this.getRef(input);
    return {
      body: readFileSync(asset.storage_path),
      mimeType: ref.declared_mime_type ?? asset.detected_mime_type ?? mimeTypeForFilename(ref.filename)
    };
  }

  syncWorkspaceFile(input: {
    user_id: string;
    workspace_id: string;
    filename: string;
    path: string;
    declared_mime_type?: string;
    session_id?: string;
    run_id?: string;
  }): ResolvedFileAssetRef {
    const content = readFileSync(input.path);
    const sha256 = hashBuffer(content);
    const storagePath = this.assetStoragePath(sha256);
    // Ensure the asset exists (dedup by sha256, same as createRef).
    let asset = this.metadataStore.fileAssets.findBySha256(sha256);
    if (!asset) {
      mkdirSync(dirname(storagePath), { recursive: true });
      const temporaryPath = `${storagePath}.${randomUUID()}.tmp`;
      writeFileSync(temporaryPath, content);
      renameSync(temporaryPath, storagePath);
      asset = this.metadataStore.fileAssets.create({
        id: randomUUID(),
        sha256,
        size_bytes: content.length,
        storage_path: storagePath,
        ...(input.declared_mime_type ? { detected_mime_type: input.declared_mime_type } : {})
      });
    }
    // Match by the sanitized workspace-relative path so a file in a subdirectory
    // (e.g. "test-output/summary.md") stays distinct from a root file of the same name.
    // Lookup is SCOPED by session: a session-scoped sync (session_id set) only matches
    // refs in the same session; a cross-session sync (session_id null/omitted) only
    // matches workspace-level refs (session_id IS NULL). Without this scoping, a session
    // file named "report.md" would hijack the workspace-root "report.md" ref via
    // reassignAsset — silently turning a cross-session asset into session content.
    const filename = safeWorkspacePath(input.filename);
    const existing = this.metadataStore.fileAssetRefs.findActiveByFilename({
      user_id: input.user_id,
      workspace_id: input.workspace_id,
      filename,
      ...(input.session_id ? { session_id: input.session_id } : { session_id: null })
    });

    if (!existing) {
      const ref = this.metadataStore.fileAssetRefs.create({
        id: randomUUID(),
        file_asset_id: asset.id,
        user_id: input.user_id,
        workspace_id: input.workspace_id,
        filename,
        ...(input.declared_mime_type ? { declared_mime_type: input.declared_mime_type } : {}),
        source: "workspace",
        ...(input.session_id ? { session_id: input.session_id } : {}),
        ...(input.run_id ? { run_id: input.run_id } : {})
      });
      return { asset, ref };
    }
    if (existing.file_asset_id === asset.id) {
      // Content unchanged — no-op.
      return { asset, ref: existing };
    }
    // Content changed — reassign the existing ref to the new asset. file_id (ref.id) is
    // stable; the previous asset becomes an orphan candidate for gcOrphanAssets.
    const reassigned = this.metadataStore.fileAssetRefs.reassignAsset({
      user_id: input.user_id,
      workspace_id: input.workspace_id,
      id: existing.id,
      file_asset_id: asset.id
    });
    return { asset, ref: reassigned };
  }

  gcOrphanAssets(): { removed: number } {
    const orphans = this.metadataStore.fileAssets.listOrphans();
    let removed = 0;
    for (const orphan of orphans) {
      try {
        this.assertStoragePath(orphan.storage_path);
        rmSync(orphan.storage_path, { force: true });
      } catch {
        // storage path invalid or already gone — still drop the record
      }
      this.metadataStore.fileAssets.hardDelete({ id: orphan.id });
      removed += 1;
    }
    return { removed };
  }

  promoteFileToWorkspace(input: {
    user_id: string;
    workspace_id: string;
    file_asset_ref_id: string;
    filename?: string;
    declared_mime_type?: string;
  }): ResolvedFileAssetRef {
    // Resolve the source ref + asset backing the artifact. The asset bytes already live
    // in the content-addressed store; promotion only adds a cross-session workspace ref
    // pointing at the same asset — no copy.
    const source = this.getRef({
      user_id: input.user_id,
      workspace_id: input.workspace_id,
      id: input.file_asset_ref_id
    });
    const filename = safeWorkspacePath(input.filename ?? source.ref.filename);
    const declaredMimeType = input.declared_mime_type
      ?? source.ref.declared_mime_type
      ?? source.asset.detected_mime_type;
    // Idempotent by filename across cross-session workspace refs (session_id IS NULL).
    // We scan all refs to find a name match, but only treat a cross-session workspace
    // ref as the reuse target — session-scoped refs are left alone.
    const existing = this.metadataStore.fileAssetRefs.findActiveByFilename({
      user_id: input.user_id,
      workspace_id: input.workspace_id,
      filename,
      source: "workspace",
      session_id: null
    });
    if (existing && existing.file_asset_id === source.asset.id) {
      // Already promoted to the same asset — no-op, return stable ref.
      return { asset: source.asset, ref: existing };
    }
    if (existing) {
      // Name taken by a cross-session workspace ref pointing at different content —
      // reassign it to the artifact's asset (file_id stays stable).
      const reassigned = this.metadataStore.fileAssetRefs.reassignAsset({
        user_id: input.user_id,
        workspace_id: input.workspace_id,
        id: existing.id,
        file_asset_id: source.asset.id
      });
      return { asset: source.asset, ref: reassigned };
    }
    const ref = this.metadataStore.fileAssetRefs.create({
      id: randomUUID(),
      file_asset_id: source.asset.id,
      user_id: input.user_id,
      workspace_id: input.workspace_id,
      filename,
      ...(declaredMimeType ? { declared_mime_type: declaredMimeType } : {}),
      source: "workspace",
      metadata_json: { promoted_from: input.file_asset_ref_id }
    });
    return { asset: source.asset, ref };
  }

  private assetStoragePath(sha256: string): string {
    const path = resolve(this.root, sha256.slice(0, 2), sha256.slice(2, 4), sha256);
    if (!path.startsWith(`${this.root}${sep}`)) {
      throw new Error("FILE_ASSET_STORAGE_PATH_INVALID");
    }
    return path;
  }

  private assertStoragePath(storagePath: string): void {
    const path = resolve(storagePath);
    if (path !== this.root && !path.startsWith(`${this.root}${sep}`)) {
      throw new Error("FILE_ASSET_STORAGE_PATH_INVALID");
    }
  }
}

export const fileAssetRefDto = (input: ResolvedFileAssetRef): Record<string, unknown> => ({
  id: input.ref.id,
  assetId: input.asset.id,
  filename: input.ref.filename,
  mimeType: input.ref.declared_mime_type ?? input.asset.detected_mime_type ?? mimeTypeForFilename(input.ref.filename),
  sizeBytes: input.asset.size_bytes,
  sha256: input.asset.sha256,
  source: input.ref.source,
  // Derived display tags (R-021). `origin` is the user-facing provenance label mapped
  // from the internal `source`; `scope` distinguishes per-session artifacts from
  // cross-session workspace assets.
  origin: fileAssetRefOrigin(input.ref.source),
  scope: input.ref.session_id ? "session" : "workspace",
  status: input.ref.status,
  ...(input.ref.session_id ? { sessionId: input.ref.session_id } : {}),
  ...(input.ref.run_id ? { runId: input.ref.run_id } : {}),
  createdAt: input.ref.created_at
});

/**
 * Map the internal FileAssetRefSource to the user-facing `origin` label used by the
 * frontend files panel (R-021): uploaded (user uploads), generated (run artifacts),
 * saved (cross-session workspace assets). Non-file-library sources keep a stable label
 * so callers can intentionally include them without treating them as reusable files.
 */
export const fileAssetRefOrigin = (source: FileAssetRefSource): string => {
  switch (source) {
    case "upload":
      return "uploaded";
    case "artifact":
      return "generated";
    case "workspace":
      return "saved";
    case "knowledge":
      return "knowledge";
    case "run-attachment":
      return "run-attachment";
    case "skill-package":
      return "skill-package";
    default:
      return "other";
  }
};

export const safeFilename = (filename: string): string =>
  basename(filename).replace(/[^a-zA-Z0-9._ -]+/gu, "-").trim() || "file";

/**
 * Sanitize a workspace-relative path for use as a ref filename. Unlike safeFilename
 * (which basenames), this preserves the directory structure so files in subdirectories
 * keep distinct names (e.g. "test-output/summary.md" vs "summary.md"). Each segment is
 * sanitized; path traversal (..) and absolute paths are rejected by collapsing to a
 * clean relative path.
 */
export const safeWorkspacePath = (value: string): string => {
  const segments = value.split(/[/\\]+/).map((segment) =>
    segment.replace(/[^a-zA-Z0-9._ -]+/gu, "-").trim()
  ).filter(Boolean);
  const cleaned = segments
    .filter((segment) => segment !== "." && segment !== "..")
    .join("/");
  return cleaned || "file";
};

export const mimeTypeForFilename = (filename: string): string => {
  const extension = filename.toLowerCase().split(".").pop();
  return ({
    csv: "text/csv; charset=utf-8",
    html: "text/html; charset=utf-8",
    json: "application/json; charset=utf-8",
    md: "text/markdown; charset=utf-8",
    png: "image/png",
    svg: "image/svg+xml",
    txt: "text/plain; charset=utf-8",
    xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
  } as Record<string, string>)[extension ?? ""] ?? "application/octet-stream";
};

const hashBuffer = (content: Buffer): string => createHash("sha256").update(content).digest("hex");
