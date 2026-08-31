import {
  SemanticProviderError,
  type SemanticProvider,
  type SemanticProviderResult,
  type SemanticRequest,
  type SemanticResolution
} from "./types.js";

export type SemanticSnapshot = {
  value: unknown;
  snapshotId: string;
  expiresAt: number;
  capabilities?: string[];
  trust?: SemanticProviderResult["trust"];
  warnings?: string[];
};

export type SemanticProviderChainOptions = {
  live: SemanticProvider;
  local: SemanticProvider;
  now?: () => number;
};

export class SemanticProviderChain {
  private readonly snapshots = new Map<string, SemanticSnapshot>();

  constructor(private readonly options: SemanticProviderChainOptions) {}

  cacheSnapshot(request: SemanticRequest, snapshot: SemanticSnapshot): void {
    this.snapshots.set(snapshotKey(request), snapshot);
  }

  async resolve(request: SemanticRequest): Promise<SemanticResolution> {
    let fallbackReason: string | undefined;
    try {
      const result = await this.options.live.resolve(request);
      return {
        ...result,
        provider: "datalink",
        mode: "live",
        datasourceRevision: request.datasourceRevision
      };
    } catch (error) {
      if (!(error instanceof SemanticProviderError) || !error.fallbackAllowed) {
        throw error;
      }
      fallbackReason = error.code;
    }
    const snapshot = this.snapshots.get(snapshotKey(request));
    if (snapshot && snapshot.expiresAt > (this.options.now?.() ?? Date.now())) {
      return {
        value: snapshot.value,
        capabilities: snapshot.capabilities ?? ["graph-explore"],
        trust: snapshot.trust ?? "inferred",
        warnings: snapshot.warnings ?? [],
        snapshotId: snapshot.snapshotId,
        provider: "datalink",
        mode: "cached",
        datasourceRevision: request.datasourceRevision,
        ...(fallbackReason ? { fallbackReason } : {})
      };
    }
    try {
      const result = await this.options.local.resolve(request);
      return {
        ...result,
        provider: "local",
        mode: "fallback",
        datasourceRevision: request.datasourceRevision,
        ...(fallbackReason ? { fallbackReason } : {})
      };
    } catch (error) {
      const reason = error instanceof SemanticProviderError ? error.code : "LOCAL_SEMANTIC_UNAVAILABLE";
      return {
        value: undefined,
        capabilities: [],
        trust: "unknown",
        warnings: [reason],
        provider: "none",
        mode: "unavailable",
        datasourceRevision: request.datasourceRevision,
        ...(fallbackReason ? { fallbackReason } : {})
      };
    }
  }
}

const snapshotKey = (request: SemanticRequest): string => [
  request.userId,
  request.workspaceId,
  request.datasourceId,
  request.datasourceRevision
].join(":");
