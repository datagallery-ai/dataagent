import {
  SemanticProviderError,
  type SemanticProvider,
  type SemanticProviderResult,
  type SemanticRequest
} from "./types.js";

export type LocalSchemaInspector = {
  inspectSchema(request: SemanticRequest): Promise<unknown>;
};

/** Provide a deterministic schema-only semantic fallback without inferred business meaning. */
export class LocalSemanticProvider implements SemanticProvider {
  readonly id = "local" as const;

  constructor(private readonly inspector: LocalSchemaInspector) {}

  async resolve(request: SemanticRequest): Promise<SemanticProviderResult> {
    const schema = request.physicalSchema ?? await this.inspector.inspectSchema(request);
    const tables = recordValue(schema, "tables");
    if (!Array.isArray(tables)) {
      throw new SemanticProviderError("LOCAL_SEMANTIC_UNAVAILABLE", true);
    }
    return {
      value: { tables },
      capabilities: ["physical-schema"],
      trust: "verified",
      warnings: ["LOCAL_SEMANTIC_LIMITED_TO_PHYSICAL_SCHEMA"]
    };
  }
}

const recordValue = (value: unknown, key: string): unknown =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)[key]
    : undefined;
