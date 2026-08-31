import type {
  ArtifactSummary,
  ArtifactType,
  ChartPreview,
  ChartPreviewPoint,
  ChartPreviewSeries,
  ChartPreviewType,
  Citation
} from "@datafoundry/contracts";
import { type FileAssetService, fileAssetRefDto, mimeTypeForFilename } from "@datafoundry/files";
import { artifactRecordToSummary, type MetadataStore } from "@datafoundry/metadata";
import { randomUUID } from "node:crypto";
import { basename } from "node:path";

export * from "./output-inclusion.js";
export * from "./session-output-service.js";

const resolveArtifactMimeType = (name: string, sourcePath: string): string => {
  const mimeFromName = mimeTypeForFilename(name);
  if (mimeFromName !== "application/octet-stream") {
    return mimeFromName;
  }
  return mimeTypeForFilename(basename(sourcePath));
};

export type CreateArtifactInput = {
  user_id: string;
  session_id: string;
  run_id: string;
  type: ArtifactType;
  name: string;
  preview_json?: unknown;
  citations?: Citation[];
  /**
   * Arbitrary metadata merged into the artifact record's `metadata_json` (R-018 uses
   * this to attach audit_log_id / tool_call_id / step_id to SQL result artifacts).
   * Merged with `citations` when both are present.
   */
  metadata_json?: unknown;
};

export type CreateArtifactFromFileInput = CreateArtifactInput & {
  source_path: string;
  workspace_id: string;
  metadata?: unknown;
};

export interface ArtifactService {
  createArtifact(input: CreateArtifactInput): Promise<ArtifactSummary>;
  createArtifactFromFile(input: CreateArtifactFromFileInput): Promise<ArtifactSummary & {
    download_url: string;
    file_id: string;
  }>;
  /**
   * Create a chart artifact with a contract-validated `ChartPreview` `preview_json`
   * (R-015). This is the backend rule-based path — there is no agent `create_chart`
   * tool; the model never assembles chart data. The frontend renders the produced
   * structure (bar/line/pie).
   *
   * R-018: callers should pass `metadata_json.tool_call_id` (and optional `step_id`)
   * whenever a tool produced the chart so restore can link without heuristics.
   */
  createChartArtifact(input: {
    user_id: string;
    session_id: string;
    run_id: string;
    name: string;
    chartType: ChartPreviewType;
    points?: ChartPreviewPoint[];
    series?: ChartPreviewSeries[];
    unit?: string;
    metadata_json?: unknown;
  }): Promise<ArtifactSummary>;
}

/**
 * Build and validate a `ChartPreview` for a chart artifact's `preview_json` (R-015).
 * Normalizes the chart kind to bar/line/pie (rejecting others), drops malformed
 * points/series, and requires at least one valid point (single-series) or one valid
 * series (multi-series). Returns undefined when there is no usable data.
 */
export const buildChartPreview = (input: {
  chartType: ChartPreviewType;
  points?: ChartPreviewPoint[];
  series?: ChartPreviewSeries[];
  unit?: string;
}): ChartPreview | undefined => {
  const points = (input.points ?? [])
    .map((point) => ({
      label: typeof point?.label === "string" ? point.label : String(point?.label ?? ""),
      value: Number.isFinite(point?.value) ? (point.value as number) : Number(point?.value)
    }))
    .filter((point) => point.label.length > 0 && Number.isFinite(point.value));
  const series = (input.series ?? [])
    .map((seriesEntry) => ({
      name: typeof seriesEntry?.name === "string" ? seriesEntry.name : String(seriesEntry?.name ?? ""),
      points: (seriesEntry?.points ?? [])
        .map((point) => ({
          label: typeof point?.label === "string" ? point.label : String(point?.label ?? ""),
          value: Number.isFinite(point?.value) ? (point.value as number) : Number(point?.value)
        }))
        .filter((point) => point.label.length > 0 && Number.isFinite(point.value))
    }))
    .filter((seriesEntry) => seriesEntry.name.length > 0 && seriesEntry.points.length > 0);
  if (points.length === 0 && series.length === 0) {
    return undefined;
  }
  return {
    chartType: input.chartType,
    ...(input.unit ? { unit: input.unit } : {}),
    points,
    ...(series.length > 0 ? { series } : {})
  };
};

/**
 * Merge a caller-supplied metadata object with citations into a single `metadata_json`
 * value for the artifact record. Returns undefined when neither is present (so the
 * column stays NULL). Caller metadata keys win over citation keys on conflict.
 */
const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const mergeArtifactMetadata = (metadata: unknown, citations?: Citation[]): unknown => {
  const hasMetadata = metadata !== undefined;
  const hasCitations = citations && citations.length > 0;
  if (!hasMetadata && !hasCitations) {
    return undefined;
  }
  const base = (hasCitations ? { citations } : {}) as Record<string, unknown>;
  if (hasMetadata && typeof metadata === "object" && metadata !== null) {
    return { ...base, ...(metadata as Record<string, unknown>) };
  }
  return hasMetadata ? { value: metadata, ...base } : base;
};

export class LocalArtifactService implements ArtifactService {
  constructor(
    private readonly metadataStore: MetadataStore,
    private readonly fileAssetService?: FileAssetService
  ) {}

  async createArtifact(input: CreateArtifactInput): Promise<ArtifactSummary> {
    const mergedMetadata = mergeArtifactMetadata(input.metadata_json, input.citations);
    const record = this.metadataStore.artifacts.create({
      user_id: input.user_id,
      session_id: input.session_id,
      run_id: input.run_id,
      id: randomUUID(),
      type: input.type,
      name: input.name,
      preview_json: input.preview_json,
      ...(mergedMetadata !== undefined ? { metadata_json: mergedMetadata } : {})
    });

    return artifactRecordToSummary(record);
  }

  async createChartArtifact(input: {
    user_id: string;
    session_id: string;
    run_id: string;
    name: string;
    chartType: ChartPreviewType;
    points?: ChartPreviewPoint[];
    series?: ChartPreviewSeries[];
    unit?: string;
    metadata_json?: unknown;
  }): Promise<ArtifactSummary> {
    const preview = buildChartPreview({
      chartType: input.chartType,
      ...(input.points ? { points: input.points } : {}),
      ...(input.series ? { series: input.series } : {}),
      ...(input.unit ? { unit: input.unit } : {})
    });
    if (!preview) {
      throw new Error("CHART_DATA_REQUIRED");
    }
    return this.createArtifact({
      user_id: input.user_id,
      session_id: input.session_id,
      run_id: input.run_id,
      type: "chart",
      name: input.name,
      preview_json: preview,
      ...(input.metadata_json !== undefined ? { metadata_json: input.metadata_json } : {})
    });
  }

  async createArtifactFromFile(input: CreateArtifactFromFileInput): Promise<ArtifactSummary & {
    download_url: string;
    file_id: string;
  }> {
    if (!this.fileAssetService) {
      throw new Error("FILE_ASSET_SERVICE_REQUIRED");
    }
    const file = this.fileAssetService.createRefFromPath({
      user_id: input.user_id,
      workspace_id: input.workspace_id,
      session_id: input.session_id,
      run_id: input.run_id,
      filename: input.name,
      declared_mime_type: resolveArtifactMimeType(input.name, input.source_path),
      source: "artifact",
      path: input.source_path,
      metadata: input.metadata
    });
    const mimeType = resolveArtifactMimeType(input.name, input.source_path);
    const record = this.metadataStore.artifacts.create({
      user_id: input.user_id,
      session_id: input.session_id,
      run_id: input.run_id,
      id: randomUUID(),
      type: input.type,
      name: input.name,
      mime_type: mimeType,
      file_asset_ref_id: file.ref.id,
      preview_json: input.preview_json,
      metadata_json: {
        ...(input.citations ? { citations: input.citations } : {}),
        ...(isRecord(input.metadata) ? input.metadata : {}),
        ...(isRecord(input.metadata_json) ? input.metadata_json : {}),
        file: fileAssetRefDto(file)
      }
    });
    return {
      ...artifactRecordToSummary(record),
      file_id: file.ref.id,
      download_url: `/api/v1/artifacts/${record.id}/download`
    };
  }
}
