export type SemanticTrust = "authoritative" | "verified" | "inferred" | "unknown";

export type SemanticRequest = {
  userId: string;
  workspaceId: string;
  datasourceId: string;
  datasourceRevision: string;
  query: string;
  physicalSchema?: unknown;
};

export type SemanticProviderResult = {
  value: unknown;
  capabilities: string[];
  trust: SemanticTrust;
  warnings: string[];
  snapshotId?: string;
};

export interface SemanticProvider {
  id: "datalink" | "local";
  resolve(request: SemanticRequest): Promise<SemanticProviderResult>;
}

export type SemanticResolution = SemanticProviderResult & {
  provider: "datalink" | "local" | "none";
  mode: "live" | "cached" | "fallback" | "unavailable";
  datasourceRevision: string;
  fallbackReason?: string;
};

export class SemanticProviderError extends Error {
  constructor(
    readonly code: string,
    readonly fallbackAllowed: boolean,
    options?: ErrorOptions
  ) {
    super(code, options);
    this.name = "SemanticProviderError";
  }
}
