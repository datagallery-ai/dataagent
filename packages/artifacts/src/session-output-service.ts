import type { ArtifactSummary, ArtifactType } from "@datafoundry/contracts";
import { type FileAssetService, fileAssetRefDto, mimeTypeForFilename } from "@datafoundry/files";
import {
  artifactRecordToSummary,
  type ArtifactRecord,
  type ArtifactVersionRecord,
  type MetadataStore
} from "@datafoundry/metadata";
import { randomUUID } from "node:crypto";
import { basename } from "node:path";

import {
  inferOutputTypeFromPath,
  normalizeSessionOutputPath,
  shouldIngestSessionOutputPath
} from "./output-inclusion.js";

export type UpsertSessionOutputFromFileInput = {
  user_id: string;
  workspace_id: string;
  session_id: string;
  run_id: string;
  path: string;
  source_path: string;
  tool_call_id?: string;
};

export type SessionOutputArtifactSummary = ArtifactSummary & {
  download_url: string;
  file_asset_ref_id: string;
  file_id: string;
  logical_key: string;
  version: number;
};

export type SessionOutputUpsertResult = {
  artifact: SessionOutputArtifactSummary;
  version: ArtifactVersionRecord;
};

export class SessionOutputService {
  constructor(
    private readonly metadataStore: MetadataStore,
    private readonly fileAssetService: FileAssetService
  ) {}

  async upsertFromSessionFile(input: UpsertSessionOutputFromFileInput): Promise<SessionOutputUpsertResult | null> {
    const normalizedPath = normalizeSessionOutputPath(input.path);
    if (!shouldIngestSessionOutputPath(normalizedPath)) {
      return null;
    }

    const logicalKey = `session_file:${normalizedPath}`;
    const type = inferOutputTypeFromPath(normalizedPath);
    const name = basename(normalizedPath);
    const mimeType = mimeTypeForFilename(name);
    const file = this.fileAssetService.createRefFromPath({
      user_id: input.user_id,
      workspace_id: input.workspace_id,
      session_id: input.session_id,
      run_id: input.run_id,
      filename: name,
      declared_mime_type: mimeType,
      source: "artifact",
      path: input.source_path,
      metadata: {
        logical_key: logicalKey,
        path: normalizedPath,
        source: "session_file",
        ...(input.tool_call_id ? { tool_call_id: input.tool_call_id } : {})
      }
    });
    const preview = createFilePreview({
      file_id: file.ref.id,
      mime_type: mimeType,
      path: normalizedPath,
      type
    });
    const metadata = {
      logical_key: logicalKey,
      path: normalizedPath,
      source: "session_file",
      ...(input.tool_call_id ? { tool_call_id: input.tool_call_id } : {}),
      file: fileAssetRefDto(file)
    };
    const existing = this.metadataStore.artifacts.findBySessionLogicalKey({
      user_id: input.user_id,
      session_id: input.session_id,
      logical_key: logicalKey
    });
    const record = existing
      ? this.metadataStore.artifacts.updateFileAssetRef({
        user_id: input.user_id,
        artifact_id: existing.id,
        run_id: input.run_id,
        type,
        name,
        mime_type: mimeType,
        file_asset_ref_id: file.ref.id,
        preview_json: preview,
        metadata_json: metadata
      })
      : this.metadataStore.artifacts.create({
        id: randomUUID(),
        user_id: input.user_id,
        session_id: input.session_id,
        run_id: input.run_id,
        type,
        name,
        mime_type: mimeType,
        file_asset_ref_id: file.ref.id,
        preview_json: preview,
        metadata_json: metadata
      });
    const version = this.metadataStore.artifactVersions.create({
      id: randomUUID(),
      user_id: input.user_id,
      artifact_id: record.id,
      file_asset_ref_id: file.ref.id,
      preview_json: preview,
      content_hash: file.asset.sha256
    });
    return {
      artifact: toSessionOutputArtifactSummary(record, logicalKey, version.version),
      version
    };
  }
}

const createFilePreview = (input: {
  file_id: string;
  mime_type: string;
  path: string;
  type: ArtifactType;
}): Record<string, unknown> => ({
  file_id: input.file_id,
  mime_type: input.mime_type,
  path: input.path,
  type: input.type
});

const toSessionOutputArtifactSummary = (
  record: ArtifactRecord,
  logicalKey: string,
  version: number
): SessionOutputArtifactSummary => {
  if (!record.file_asset_ref_id) {
    throw new Error(`SESSION_OUTPUT_FILE_REF_REQUIRED:${record.id}`);
  }
  return {
    ...artifactRecordToSummary(record),
    file_asset_ref_id: record.file_asset_ref_id,
    file_id: record.file_asset_ref_id,
    download_url: `/api/v1/artifacts/${record.id}/download`,
    logical_key: logicalKey,
    version
  };
};
