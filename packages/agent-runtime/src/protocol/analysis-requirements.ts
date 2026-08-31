import {
  createAnalysisAssertions,
  createManualAnalysisAssertion,
  type AnalysisAssertion,
  type AnalysisAssertionDraft,
  type AnalysisClaimValue,
  type AnalysisValidationFinding,
  type AnalysisVerifiedValue
} from "./analysis-contract.js";

export const ANALYSIS_REQUIREMENT_KINDS = [
  "data_quality",
  "metric",
  "segmentation",
  "comparison",
  "validation",
  "counterfactual",
  "decision",
  "deliverable"
] as const;

export type AnalysisRequirementKind = typeof ANALYSIS_REQUIREMENT_KINDS[number];
export type AnalysisRequirementStatus = "pending" | "queried" | "validated" | "evidenced" | "reported";

export type AnalysisRequirement = {
  id: string;
  kind: AnalysisRequirementKind;
  description: string;
  acceptanceCriteria: string[];
  assertions: AnalysisAssertion[];
  required: boolean;
  source: "protocol" | "user";
  status: AnalysisRequirementStatus;
  taskIds: string[];
  queryAttemptIds: string[];
  evidenceBindingIds: string[];
  reportedClaimIds: string[];
};

export type AnalysisQueryAttempt = {
  id: string;
  requirementIds: string[];
  assertionIds: string[];
  assertions: AnalysisAssertion[];
  sql?: string;
  expectedColumns: string[];
  status: "planned" | "validated" | "executed" | "evidenced";
  valid: boolean;
  artifactId?: string;
  auditLogId?: string;
  resultFields: string[];
  validationFindings: AnalysisValidationFinding[];
  resultValidationFindings: AnalysisValidationFinding[];
  verifiedValues: AnalysisVerifiedValue[];
};

export type AnalysisEvidenceBinding = {
  id: string;
  requirementId: string;
  queryAttemptId: string;
  artifactId: string;
  auditLogId: string;
  resultFields: string[];
  validationStatus: "passed";
};

export type AnalysisReportedClaim = {
  id: string;
  requirementId: string;
  claim: string;
  evidenceBindingIds: string[];
  values: AnalysisClaimValue[];
};

export type TaskRequirementLink = {
  taskId: string;
  requirementIds: string[];
};

export type AnalysisRequirementDraft = {
  kind: AnalysisRequirementKind;
  description: string;
  acceptanceCriteria: string[];
  assertions?: AnalysisAssertionDraft[];
};

/** Create deterministic protocol-owned requirements that models cannot remove. */
export const createCoreAnalysisRequirements = (): AnalysisRequirement[] => [
  createRequirement("CORE_SCHEMA", "validation", "Inspect and ground the physical schema", "protocol"),
  createRequirement("CORE_SEMANTIC", "validation", "Resolve semantic context or disclose fallback", "protocol"),
  createRequirement("CORE_QUERY", "validation", "Validate the current read-only query", "protocol"),
  createRequirement("CORE_RESULT", "validation", "Validate the current query result", "protocol"),
  createRequirement("CORE_EVIDENCE", "validation", "Bind audited evidence for the current result", "protocol")
];

/** Normalize model drafts into stable server-owned user requirement records. */
export const createUserAnalysisRequirements = (drafts: AnalysisRequirementDraft[]): AnalysisRequirement[] => {
  const seen = new Set<string>();
  const uniqueDrafts: AnalysisRequirementDraft[] = [];
  for (const draft of drafts) {
    const description = draft.description.trim();
    const key = description.toLocaleLowerCase();
    if (!description || seen.has(key)) {
      continue;
    }
    seen.add(key);
    uniqueDrafts.push({ ...draft, description });
  }
  return uniqueDrafts.map((draft, index) => {
    const id = `R${index + 1}`;
    return {
      ...createRequirement(id, draft.kind, draft.description, "user"),
      acceptanceCriteria: [...draft.acceptanceCriteria],
      assertions: draft.assertions && draft.assertions.length > 0
        ? createAnalysisAssertions(id, draft.assertions)
        : [createManualAnalysisAssertion(id, draft.description)]
    };
  });
};

const createRequirement = (
  id: string,
  kind: AnalysisRequirementKind,
  description: string,
  source: AnalysisRequirement["source"]
): AnalysisRequirement => ({
  id,
  kind,
  description,
  acceptanceCriteria: [],
  assertions: [],
  required: true,
  source,
  status: "pending",
  taskIds: [],
  queryAttemptIds: [],
  evidenceBindingIds: [],
  reportedClaimIds: []
});
