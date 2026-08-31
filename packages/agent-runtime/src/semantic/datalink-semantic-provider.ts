import {
  SemanticProviderError,
  type SemanticProvider,
  type SemanticProviderResult,
  type SemanticRequest,
  type SemanticTrust
} from "./types.js";

export type DataLinkToolClient = {
  callTool(name: string, args: Record<string, unknown>): Promise<unknown>;
};

/** Resolve semantic context through the DataLink MCP explore capability. */
export class DataLinkSemanticProvider implements SemanticProvider {
  readonly id = "datalink" as const;

  constructor(private readonly client: DataLinkToolClient) {}

  async resolve(request: SemanticRequest): Promise<SemanticProviderResult> {
    try {
      const value = await this.client.callTool("datalink_explore", {
        query: request.query,
        mask_credential: true
      });
      if (typeof value === "string" && /^Error executing tool\b/iu.test(value)) {
        throw new Error(value);
      }
      if (isEmptySemanticResult(value)) {
        throw new SemanticProviderError("DATALINK_EMPTY_RESULT", true);
      }
      return {
        value,
        capabilities: ["graph-explore"],
        trust: inferTrust(value),
        warnings: [],
        ...optionalSnapshotId(value)
      };
    } catch (error) {
      throw mapDataLinkError(error);
    }
  }
}

const inferTrust = (value: unknown): SemanticTrust => {
  if (typeof value === "string" && value.trim().length > 0) return "inferred";
  const nodes = recordValue(value, "nodes");
  if (!Array.isArray(nodes) || nodes.length === 0) return "unknown";
  const sources = nodes.map((node) => recordString(node, "source"));
  if (sources.every((source) => source === "authoritative")) return "authoritative";
  if (sources.every((source) => source === "verified" || source === "authoritative")) return "verified";
  return "inferred";
};

const optionalSnapshotId = (value: unknown): { snapshotId?: string } => {
  const snapshotId = recordString(value, "snapshot_id");
  return snapshotId ? { snapshotId } : {};
};

const mapDataLinkError = (error: unknown): SemanticProviderError => {
  if (error instanceof SemanticProviderError) {
    return error;
  }
  const message = error instanceof Error ? error.message : String(error);
  if (/\b(?:401|403)\b|unauthori[sz]ed|forbidden/i.test(message)) {
    return new SemanticProviderError("DATALINK_NOT_AUTHORIZED", false, { cause: error });
  }
  if (/\b(?:400|404|409|422)\b|policy|invalid request/i.test(message)) {
    return new SemanticProviderError("DATALINK_REQUEST_REJECTED", false, { cause: error });
  }
  return new SemanticProviderError("DATALINK_UNAVAILABLE", true, { cause: error });
};

const isEmptySemanticResult = (value: unknown): boolean => {
  if (typeof value === "string") {
    const text = value.trim();
    return text.length === 0 || /^No results found\b/iu.test(text);
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return value === undefined || value === null;
  }
  const nodes = recordValue(value, "nodes");
  if (Array.isArray(nodes)) {
    return nodes.length === 0;
  }
  const content = recordValue(value, "content");
  return typeof content === "string" && isEmptySemanticResult(content);
};

const recordValue = (value: unknown, key: string): unknown =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)[key]
    : undefined;

const recordString = (value: unknown, key: string): string | undefined => {
  const field = recordValue(value, key);
  return typeof field === "string" && field.length > 0 ? field : undefined;
};
