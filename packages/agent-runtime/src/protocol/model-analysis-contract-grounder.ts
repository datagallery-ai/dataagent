import { Agent } from "@mastra/core/agent";
import type { ModelProvider } from "@datafoundry/providers";
import { z } from "zod";

import { AGENT_RUNTIME_LIMITS } from "../config/agent-runtime-limits.js";
import {
  analysisAssertionDraftSchema,
  createAnalysisAssertions,
  createManualAnalysisAssertion,
  type AnalysisAssertionDraft,
  type SqlSemanticConstraint
} from "./analysis-contract.js";
import type { AnalysisRequirement } from "./analysis-requirements.js";
import type { SemanticResolution } from "../semantic/types.js";

const groundedContractSchema = z.object({
  requirementId: z.string().min(1),
  assertions: z.array(analysisAssertionDraftSchema).min(1).max(12)
}).strict().superRefine((contract, context) => {
  contract.assertions.forEach((assertion, index) => {
    if (assertion.kind !== "manual" && (assertion.claimValues?.length ?? 0) === 0) {
      context.addIssue({
        code: "custom",
        message: "Structured assertions require at least one claimValues entry.",
        path: ["assertions", index, "claimValues"]
      });
    }
  });
});

const groundingSchema = z.object({
  contracts: z.array(groundedContractSchema).max(16)
}).strict();

export type AnalysisContractGroundingInput = {
  requirements: AnalysisRequirement[];
  physicalSchema: unknown;
  semanticResolution: SemanticResolution;
  datasourceRevision: string;
};

export type AnalysisContractGroundingFinding = {
  requirementId: string;
  code: "CONTRACT_MISSING" | "CONTRACT_INVALID_OUTPUT" | "CONTRACT_UNKNOWN_TABLE" | "CONTRACT_UNKNOWN_COLUMN";
  message: string;
};

export type AnalysisContractGroundingResult = {
  requirements: AnalysisRequirement[];
  findings: AnalysisContractGroundingFinding[];
};

export type AnalysisContractGrounder = (
  input: AnalysisContractGroundingInput
) => Promise<AnalysisContractGroundingResult>;

/** Build a schema-bound prompt for converting logical requirements into physical assertions. */
export const createAnalysisContractGroundingPrompt = (input: AnalysisContractGroundingInput): string => [
  "你是数据分析 Contract grounding 器，不执行 SQL，也不回答用户问题。",
  "根据已检查的物理 schema 和语义解析结果，把每个用户 requirement 转为可验证的结构化 assertions。",
  "不得发明表名或字段名；sourceTables、dimensions 和 sqlConstraints 中的物理标识必须逐字存在于 schema。",
  "语义信息只用于选择正确字段和解释口径，不能覆盖物理 schema。没有足够依据时使用 kind=manual。",
  `datasourceRevision: ${input.datasourceRevision}`,
  `requirements: ${JSON.stringify(logicalRequirements(input.requirements))}`,
  `physicalSchema: ${JSON.stringify(input.physicalSchema)}`,
  `semanticResolution: ${JSON.stringify(input.semanticResolution)}`,
  "只返回合法 JSON 对象，不要 Markdown。顶层字段为 contracts。",
  "contracts 每项包含 requirementId 和 assertions；requirementId 必须来自输入，不得生成新 ID。",
  "每个 assertion 必须包含 kind 和 description；可选字段为 sourceTables、dimensions、sqlConstraints、",
  "resultChecks、claimValues。所有复数字段都必须是数组。",
  "sqlConstraints 仅可使用 source、column、aggregate、group_by、filter、time_range 结构。",
  "每个 sqlConstraints 元素只能表达一种 kind，不得把 source、column、aggregate 合并到同一对象。",
  '合法 sqlConstraints 元素示例: {"kind":"source","table":"<schema table>"}, '
    + '{"kind":"column","column":"<schema column>"}, '
    + '{"kind":"aggregate","function":"COUNT","column":"*","alias":"row_count"}。',
  '"*" 只能作为 aggregate 的 column，绝不能用于 kind=column、dimensions、filter 或 time_range。',
  '合法 resultChecks 元素示例: {"kind":"non_empty","required":true}, '
    + '{"kind":"not_null","required":true,"fields":["row_count"]}。不得使用字符串或 type 字段。',
  '合法 claimValues 元素示例: {"name":"row_count","field":"row_count","required":true}。'
    + "name、field、required 缺一不可。",
  "每个非 manual assertion 至少包含一个 claimValues 元素，否则无法把 SQL 结果绑定为运行时验证值。",
  "尖括号中的示例物理标识必须替换为 physicalSchema 中逐字存在的真实标识，不得原样输出。",
  "不要为 assertion 生成 id，服务端会分配稳定 ID。"
].join("\n");

/** Parse model grounding output, reject invented identifiers, and preserve requirement state. */
export const parseAnalysisContractGroundingText = (
  text: string,
  requirements: AnalysisRequirement[],
  physicalSchema: unknown
): AnalysisContractGroundingResult => {
  const trimmed = text.trim();
  const unfenced = trimmed.startsWith("```")
    ? trimmed.replace(/^```(?:json)?\s*/iu, "").replace(/\s*```$/u, "")
    : trimmed;
  const parsed = groundingSchema.parse(JSON.parse(unfenced) as unknown);
  const contracts = new Map(parsed.contracts.map((contract) => [
    contract.requirementId,
    contract.assertions.map(normalizeAssertionDraft)
  ]));
  const schema = inspectPhysicalSchema(physicalSchema);
  const findings: AnalysisContractGroundingFinding[] = [];
  const grounded = requirements.map((requirement) => {
    if (requirement.source !== "user") {
      return cloneRequirement(requirement);
    }
    const drafts = contracts.get(requirement.id);
    if (!drafts) {
      findings.push({
        requirementId: requirement.id,
        code: "CONTRACT_MISSING",
        message: `No grounded contract was returned for ${requirement.id}.`
      });
      return withManualAssertion(requirement);
    }
    const draftFindings = validateAssertionDrafts(requirement.id, drafts, schema);
    findings.push(...draftFindings);
    return draftFindings.length > 0
      ? withManualAssertion(requirement)
      : { ...cloneRequirement(requirement), assertions: createAnalysisAssertions(requirement.id, drafts) };
  });
  return { requirements: grounded, findings };
};

/** Convert a parser failure into concrete model-facing repair instructions for the next grounding attempt. */
export const createAnalysisContractGroundingRetryInstruction = (error: unknown): string => {
  return [
    "",
    "上次输出未通过服务端 schema 校验。不要重复原来的无效结构，只修正以下问题：",
    describeAnalysisContractGroundingError(error),
    "重新检查每个嵌套对象的 kind 和必填字段，并严格返回紧凑 JSON。"
  ].join("\n");
};

/** Preserve an exhausted grounding failure as an explicit manual contract with actionable findings. */
export const createFallbackAnalysisContractGrounding = (
  requirements: AnalysisRequirement[],
  error?: unknown
): AnalysisContractGroundingResult => ({
  requirements: requirements.map((requirement) => requirement.source === "user"
    ? withManualAssertion(requirement)
    : cloneRequirement(requirement)),
  findings: requirements
    .filter((requirement) => requirement.source === "user")
    .map((requirement) => ({
      requirementId: requirement.id,
      code: error === undefined ? "CONTRACT_MISSING" as const : "CONTRACT_INVALID_OUTPUT" as const,
      message: error === undefined
        ? `No valid grounded contract was produced for ${requirement.id}.`
        : `Grounding output was invalid for ${requirement.id}: ${describeAnalysisContractGroundingError(error)}`
    }))
});

/** Create a tool-free model grounder backed by the configured run model. */
export const createModelAnalysisContractGrounder = (
  provider: Exclude<ModelProvider, { kind: "mock" }>
): AnalysisContractGrounder => {
  const agent = new Agent({
    id: "analysis-contract-grounder",
    name: "Analysis Contract Grounder",
    instructions: "Ground analysis requirements against supplied schema only. Never execute or answer the task.",
    model: provider.model as never
  });
  return async (input) => {
    const prompt = createAnalysisContractGroundingPrompt(input);
    let retryInstruction = "";
    let lastError: unknown;
    for (let attempt = 0; attempt < AGENT_RUNTIME_LIMITS.contractGrounderMaxAttempts; attempt += 1) {
      const output = await agent.generate(`${prompt}${retryInstruction}`, {
        maxSteps: AGENT_RUNTIME_LIMITS.modelHelperMaxSteps,
        modelSettings: {
          maxOutputTokens: AGENT_RUNTIME_LIMITS.contractGrounderMaxOutputTokens,
          temperature: 0
        }
      });
      try {
        return parseAnalysisContractGroundingText(output.text, input.requirements, input.physicalSchema);
      } catch (error) {
        lastError = error;
        retryInstruction = createAnalysisContractGroundingRetryInstruction(error);
      }
    }
    return createFallbackAnalysisContractGrounding(input.requirements, lastError);
  };
};

const logicalRequirements = (requirements: AnalysisRequirement[]): unknown[] => requirements
  .filter((requirement) => requirement.source === "user")
  .map((requirement) => ({
    id: requirement.id,
    kind: requirement.kind,
    description: requirement.description,
    acceptanceCriteria: requirement.acceptanceCriteria
  }));

const normalizeAssertionDraft = (
  draft: z.infer<typeof analysisAssertionDraftSchema>
): AnalysisAssertionDraft => structuredClone(draft) as AnalysisAssertionDraft;

const describeAnalysisContractGroundingError = (error: unknown): string => error instanceof z.ZodError
  ? error.issues.map((issue) => `${issue.path.join(".") || "root"}: ${issue.message}`).join("; ")
  : error instanceof Error
    ? error.message
    : String(error ?? "unknown validation error");

const withManualAssertion = (requirement: AnalysisRequirement): AnalysisRequirement => ({
  ...cloneRequirement(requirement),
  assertions: [createManualAnalysisAssertion(requirement.id, requirement.description)]
});

const cloneRequirement = (requirement: AnalysisRequirement): AnalysisRequirement => ({
  ...requirement,
  acceptanceCriteria: [...requirement.acceptanceCriteria],
  assertions: structuredClone(requirement.assertions),
  taskIds: [...requirement.taskIds],
  queryAttemptIds: [...requirement.queryAttemptIds],
  evidenceBindingIds: [...requirement.evidenceBindingIds],
  reportedClaimIds: [...requirement.reportedClaimIds]
});

type PhysicalSchemaIndex = { tables: Set<string>; columns: Set<string> };

const inspectPhysicalSchema = (physicalSchema: unknown): PhysicalSchemaIndex => {
  const tables = new Set<string>();
  const columns = new Set<string>();
  const rawTables = recordValue(physicalSchema, "tables");
  if (!Array.isArray(rawTables)) {
    return { tables, columns };
  }
  for (const rawTable of rawTables) {
    const tableName = typeof rawTable === "string" ? rawTable : recordString(rawTable, "name", "table_name");
    if (tableName) {
      tables.add(normalizeIdentifier(tableName));
    }
    const rawColumns = recordValue(rawTable, "columns");
    if (!Array.isArray(rawColumns)) {
      continue;
    }
    for (const rawColumn of rawColumns) {
      const columnName = typeof rawColumn === "string" ? rawColumn : recordString(rawColumn, "name", "column_name");
      if (columnName) {
        columns.add(normalizeIdentifier(columnName));
      }
    }
  }
  return { tables, columns };
};

const validateAssertionDrafts = (
  requirementId: string,
  drafts: AnalysisAssertionDraft[],
  schema: PhysicalSchemaIndex
): AnalysisContractGroundingFinding[] => {
  const findings: AnalysisContractGroundingFinding[] = [];
  const tableNames = drafts.flatMap((draft) => [
    ...(draft.sourceTables ?? []),
    ...(draft.sqlConstraints ?? []).flatMap((constraint) => constraint.kind === "source" ? [constraint.table] : [])
  ]);
  const columnNames = drafts.flatMap((draft) => [
    ...(draft.dimensions ?? []),
    ...(draft.sqlConstraints ?? []).flatMap(constraintColumns)
  ]);
  for (const table of new Set(tableNames)) {
    if (!schema.tables.has(normalizeIdentifier(table))) {
      findings.push({
        requirementId,
        code: "CONTRACT_UNKNOWN_TABLE",
        message: `Table '${table}' does not exist in the inspected schema.`
      });
    }
  }
  for (const column of new Set(columnNames.filter((value) => value !== "*"))) {
    if (!schema.columns.has(normalizeIdentifier(column))) {
      findings.push({
        requirementId,
        code: "CONTRACT_UNKNOWN_COLUMN",
        message: `Column '${column}' does not exist in the inspected schema.`
      });
    }
  }
  return findings;
};

const constraintColumns = (constraint: SqlSemanticConstraint): string[] => {
  if (constraint.kind === "column" || constraint.kind === "filter" || constraint.kind === "time_range") {
    return [constraint.column];
  }
  if (constraint.kind === "aggregate") {
    return constraint.column ? [constraint.column] : [];
  }
  return constraint.kind === "group_by" ? constraint.columns : [];
};

const normalizeIdentifier = (value: string): string => value.trim().toLocaleLowerCase();

const recordValue = (value: unknown, key: string): unknown =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)[key]
    : undefined;

const recordString = (value: unknown, ...keys: string[]): string | undefined => {
  for (const key of keys) {
    const field = recordValue(value, key);
    if (typeof field === "string" && field.length > 0) {
      return field;
    }
  }
  return undefined;
};
