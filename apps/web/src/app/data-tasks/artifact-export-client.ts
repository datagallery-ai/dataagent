import { configApi, getConfigApiBaseUrl } from "../../lib/config-api";
import type { ArtifactExportFormat, JobDto } from "../../lib/config-api";
import { hasCapability } from "./data-task-state";

export type ArtifactExportMeta = {
  id: string;
  name: string;
  type: string;
  preview?: Record<string, unknown>;
};

export const artifactExportClient = {
  isReady(): boolean {
    return hasCapability("artifact.export");
  },

  getBaseUrl(): string {
    return getConfigApiBaseUrl();
  },

  async getArtifactMeta(id: string): Promise<ArtifactExportMeta> {
    const artifact = await configApi.getArtifact(id);
    return {
      id: artifact.id,
      name: artifact.name ?? artifact.id,
      type: artifact.type ?? "file",
      preview:
        artifact.preview_json && typeof artifact.preview_json === "object"
          ? (artifact.preview_json as Record<string, unknown>)
          : undefined,
    };
  },

  async fetchPreview(id: string): Promise<Record<string, unknown>> {
    return configApi.getArtifactPreview(id);
  },

  async download(
    id: string,
    format?: ArtifactExportFormat,
  ): Promise<{ blob: Blob; filename: string }> {
    return configApi.downloadArtifact(id, format);
  },

  async export(id: string, format: ArtifactExportFormat): Promise<JobDto> {
    return configApi.exportArtifact(id, format, `${id}:${format}`);
  },

  previewUrl(id: string): string {
    return `${getConfigApiBaseUrl()}/api/v1/artifacts/${encodeURIComponent(id)}/preview`;
  },

  downloadUrl(id: string, format?: ArtifactExportFormat): string {
    const query = format ? `?format=${encodeURIComponent(format)}` : "";
    return `${getConfigApiBaseUrl()}/api/v1/artifacts/${encodeURIComponent(id)}/download${query}`;
  },

  /** Inline content URL (Content-Disposition: inline) — used as <img>/<iframe> src. */
  contentUrl(id: string): string {
    return `${getConfigApiBaseUrl()}/api/v1/artifacts/${encodeURIComponent(id)}/content`;
  },
};
