import { z } from "zod";

export const ANALYSIS_ASSERTION_KINDS = [
  "metric",
  "filter",
  "grain",
  "comparison",
  "reconciliation",
  "counterfactual",
  "decision",
  "manual"
] as const;

export type AnalysisAssertionKind = typeof ANALYSIS_ASSERTION_KINDS[number];
export type AnalysisScalar = string | number | boolean | null;
export type AnalysisSelector = Record<string, Exclude<AnalysisScalar, null>>;

export type SqlSemanticConstraint =
  | { kind: "source"; table: string }
  | { kind: "column"; column: string }
  | { kind: "aggregate"; function: string; column?: string; alias?: string }
  | { kind: "group_by"; columns: string[] }
  | { kind: "filter"; column: string; operator: "eq" | "gt" | "gte" | "lt" | "lte"; value: AnalysisScalar }
  | { kind: "time_range"; column: string; start: string; end: string; endInclusive: boolean };

export type AnalysisValueOperand = {
  field?: string;
  literal?: AnalysisScalar;
  selector?: AnalysisSelector;
};

export type AnalysisResultCheck =
  | { kind: "non_empty"; required: boolean }
  | { kind: "row_count"; required: boolean; min?: number; max?: number }
  | { kind: "not_null"; required: boolean; fields: string[] }
  | { kind: "unique"; required: boolean; fields: string[] }
  | {
      kind: "equals" | "comparison" | "budget_conservation";
      required: boolean;
      left: AnalysisValueOperand;
      right: AnalysisValueOperand;
      operator?: "eq" | "gt" | "gte" | "lt" | "lte";
      tolerance?: number;
    }
  | {
      kind: "sum";
      required: boolean;
      total: AnalysisValueOperand;
      parts: AnalysisValueOperand[];
      tolerance?: number;
    }
  | {
      kind: "ratio";
      required: boolean;
      value: AnalysisValueOperand;
      numerator: AnalysisValueOperand;
      denominator: AnalysisValueOperand;
      scale?: number;
      tolerance?: number;
    };

export type AnalysisClaimValueSpec = {
  name: string;
  field: string;
  selector?: AnalysisSelector;
  unit?: string;
  required: boolean;
  tolerance?: number;
};

export type AnalysisAssertionDraft = {
  id?: string;
  kind: AnalysisAssertionKind;
  description: string;
  required?: boolean;
  sourceTables?: string[];
  dimensions?: string[];
  sqlConstraints?: SqlSemanticConstraint[];
  resultChecks?: AnalysisResultCheck[];
  claimValues?: AnalysisClaimValueSpec[];
};

export type AnalysisAssertion = {
  id: string;
  requirementId: string;
  kind: AnalysisAssertionKind;
  description: string;
  required: boolean;
  sourceTables: string[];
  dimensions: string[];
  sqlConstraints: SqlSemanticConstraint[];
  resultChecks: AnalysisResultCheck[];
  claimValues: AnalysisClaimValueSpec[];
};

export type AnalysisQuerySpec = {
  assertionIds: string[];
};

export type AnalysisValidationFinding = {
  code: string;
  message: string;
  severity: "error" | "warning";
  assertionId?: string;
};

export type AnalysisVerifiedValue = {
  name: string;
  value: AnalysisScalar;
  unit?: string;
  tolerance: number;
  assertionId: string;
};

export type AnalysisClaimValue = {
  name: string;
  value: AnalysisScalar;
  unit?: string;
};

const analysisScalarSchema = z.union([z.string(), z.number(), z.boolean(), z.null()]);
const analysisSelectorSchema = z.record(z.string(), z.union([z.string(), z.number(), z.boolean()]));
const analysisOperandSchema = z.object({
  field: z.string().min(1).optional(),
  literal: analysisScalarSchema.optional(),
  selector: analysisSelectorSchema.optional()
}).refine((value) => value.field !== undefined || value.literal !== undefined, {
  message: "An operand requires field or literal."
});
const sqlConstraintSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("source"), table: z.string().min(1) }),
  z.object({
    kind: z.literal("column"),
    column: z.string().min(1).refine((value) => value !== "*", {
      message: "Wildcard '*' is only valid as an aggregate operand."
    })
  }),
  z.object({
    kind: z.literal("aggregate"),
    function: z.string().min(1),
    column: z.string().min(1).optional(),
    alias: z.string().min(1).optional()
  }),
  z.object({ kind: z.literal("group_by"), columns: z.array(z.string().min(1)).min(1) }),
  z.object({
    kind: z.literal("filter"),
    column: z.string().min(1),
    operator: z.enum(["eq", "gt", "gte", "lt", "lte"]),
    value: analysisScalarSchema
  }),
  z.object({
    kind: z.literal("time_range"),
    column: z.string().min(1),
    start: z.string().min(1),
    end: z.string().min(1),
    endInclusive: z.boolean()
  })
]);
const commonBinaryCheckFields = {
  required: z.boolean(),
  left: analysisOperandSchema,
  right: analysisOperandSchema,
  tolerance: z.number().nonnegative().optional()
};
const resultCheckSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("non_empty"), required: z.boolean() }),
  z.object({
    kind: z.literal("row_count"),
    required: z.boolean(),
    min: z.number().int().nonnegative().optional(),
    max: z.number().int().nonnegative().optional()
  }),
  z.object({ kind: z.literal("not_null"), required: z.boolean(), fields: z.array(z.string().min(1)).min(1) }),
  z.object({ kind: z.literal("unique"), required: z.boolean(), fields: z.array(z.string().min(1)).min(1) }),
  z.object({ kind: z.literal("equals"), ...commonBinaryCheckFields }),
  z.object({
    kind: z.literal("sum"),
    required: z.boolean(),
    total: analysisOperandSchema,
    parts: z.array(analysisOperandSchema).min(1)
  }),
  z.object({
    kind: z.literal("comparison"),
    ...commonBinaryCheckFields,
    operator: z.enum(["eq", "gt", "gte", "lt", "lte"])
  }),
  z.object({ kind: z.literal("budget_conservation"), ...commonBinaryCheckFields }),
  z.object({
    kind: z.literal("ratio"),
    required: z.boolean(),
    value: analysisOperandSchema,
    numerator: analysisOperandSchema,
    denominator: analysisOperandSchema,
    scale: z.number().optional(),
    tolerance: z.number().nonnegative().optional()
  })
]);
const claimValueSpecSchema = z.object({
  name: z.string().min(1),
  field: z.string().min(1),
  selector: analysisSelectorSchema.optional(),
  unit: z.string().min(1).optional(),
  required: z.boolean(),
  tolerance: z.number().nonnegative().optional()
});

export const analysisAssertionDraftSchema = z.object({
  id: z.string().optional(),
  kind: z.enum(ANALYSIS_ASSERTION_KINDS),
  description: z.string().min(1).max(500),
  required: z.boolean().optional(),
  sourceTables: z.array(z.string().min(1)).optional(),
  dimensions: z.array(z.string().min(1)).optional(),
  sqlConstraints: z.array(sqlConstraintSchema).optional(),
  resultChecks: z.array(resultCheckSchema).optional(),
  claimValues: z.array(claimValueSpecSchema).optional()
}).strict();

/** Assign stable server-owned IDs and normalize model-proposed analysis assertions. */
export const createAnalysisAssertions = (
  requirementId: string,
  drafts: AnalysisAssertionDraft[]
): AnalysisAssertion[] => drafts.map((draft, index) => ({
  id: `${requirementId}.A${index + 1}`,
  requirementId,
  kind: draft.kind,
  description: draft.description.trim(),
  required: draft.required !== false,
  sourceTables: uniqueStrings(draft.sourceTables ?? []),
  dimensions: uniqueStrings(draft.dimensions ?? []),
  sqlConstraints: structuredClone(draft.sqlConstraints ?? []),
  resultChecks: structuredClone(draft.resultChecks ?? []),
  claimValues: structuredClone(draft.claimValues ?? [])
}));

/** Represent an unexpressed requirement explicitly instead of claiming deterministic verification. */
export const createManualAnalysisAssertion = (
  requirementId: string,
  description: string
): AnalysisAssertion => createAnalysisAssertions(requirementId, [{
  kind: "manual",
  description
}])[0] as AnalysisAssertion;

/** Resolve selected assertion IDs and reject cross-requirement or incomplete references. */
export const resolveRequirementAssertions = (
  requirements: Array<{ id: string; assertions: AnalysisAssertion[] }>,
  requirementIds: string[],
  assertionIds: string[]
): AnalysisAssertion[] => {
  const selectedRequirements = requirements.filter((requirement) => requirementIds.includes(requirement.id));
  const allowedAssertions = selectedRequirements.flatMap((requirement) => requirement.assertions);
  for (const assertionId of assertionIds) {
    if (!allowedAssertions.some((assertion) => assertion.id === assertionId)) {
      throw new Error(`ANALYSIS_ASSERTION_NOT_FOUND:${assertionId}`);
    }
  }
  for (const requirement of selectedRequirements) {
    if (!assertionIds.some((assertionId) => requirement.assertions.some((assertion) => assertion.id === assertionId))) {
      throw new Error(`ANALYSIS_ASSERTION_IDS_REQUIRED:${requirement.id}`);
    }
  }
  return allowedAssertions.filter((assertion) => assertionIds.includes(assertion.id));
};

const uniqueStrings = (values: string[]): string[] => [...new Set(values.map((value) => value.trim()).filter(Boolean))];
