import {
  createCoreAnalysisRequirements,
  type AnalysisEvidenceBinding,
  type AnalysisQueryAttempt,
  type AnalysisReportedClaim,
  type AnalysisRequirement,
  type TaskRequirementLink
} from "../analysis-requirements.js";
import {
  resolveRequirementAssertions,
  type AnalysisAssertion,
  type AnalysisClaimValue,
  type AnalysisScalar,
  type AnalysisVerifiedValue
} from "../analysis-contract.js";
import { DATA_ACTIONS } from "../data-actions.js";
import { validateSqlSemantics } from "../sql-semantic-validator.js";
import type { AgentProtocolDefinition } from "../types.js";

export type DataAnalysisState = {
  schemaInspected: boolean;
  datasourceDialect?: string;
  semanticResolved: boolean;
  semanticMode?: string;
  semanticTrust?: string;
  semanticWarnings: string[];
  contractGrounded: boolean;
  contractDatasourceRevision?: string;
  contractSchemaId?: string;
  contractGroundingFindings: Array<{ requirementId?: string; code?: string; message?: string }>;
  queryPlanned: boolean;
  queryValidated: boolean;
  currentQueryValidated: boolean;
  queryExecuted: boolean;
  validationPassed: boolean;
  currentEvidenceRefs: string[];
  evidenceRefs: string[];
  requirements: AnalysisRequirement[];
  queryAttempts: AnalysisQueryAttempt[];
  currentQueryAttemptId?: string;
  evidenceBindings: AnalysisEvidenceBinding[];
  reportedClaims: AnalysisReportedClaim[];
  taskRequirementLinks: TaskRequirementLink[];
};

export const createDataAnalysisProtocol = (
  availableActionNames: string[],
  userRequirements: AnalysisRequirement[] = []
): AgentProtocolDefinition<DataAnalysisState> => {
  const commonActions = unique([
    ...availableActionNames.filter((actionName) => !DATA_ACTIONS.has(actionName)),
    "protocol.handoff.propose"
  ]);
  return {
    id: "data-analysis",
    version: "1",
    initialPhase: "scope",
    phases: {
      scope: {
        allowedActions: unique([
          ...commonActions,
          "list_data_sources",
          "inspect_schema"
        ]),
        transitions: [{ targetPhase: "semantic_grounding", when: ({ state }) => state.schemaInspected }]
      },
      semantic_grounding: {
        allowedActions: unique([
          ...commonActions,
          "inspect_schema",
          "preview_table",
          "semantic.context.resolve",
          "analysis.contract.ground"
        ]),
        transitions: [{
          targetPhase: "query_planning",
          when: ({ state }) => state.semanticResolved && state.contractGrounded
        }]
      },
      query_planning: {
        allowedActions: unique([
          ...commonActions,
          "inspect_schema",
          "preview_table",
          "semantic.context.resolve",
          "data.query.plan",
          "data.query.validate"
        ]),
        transitions: [{ targetPhase: "execution", when: ({ state }) => state.currentQueryValidated }]
      },
      execution: {
        allowedActions: unique([
          ...commonActions,
          "inspect_schema",
          "semantic.context.resolve",
          "preview_table",
          "run_sql_readonly",
          "data.query.plan"
        ]),
        transitions: [
          { targetPhase: "query_planning", when: ({ actionName }) => actionName === "data.query.plan" },
          {
            targetPhase: "validation",
            when: ({ actionName, state }) => actionName === "run_sql_readonly" && state.queryExecuted
          }
        ]
      },
      validation: {
        allowedActions: unique([
          ...commonActions,
          "inspect_schema",
          "semantic.context.resolve",
          "preview_table",
          "data.query.plan",
          "analysis.result.validate",
          "analysis.evidence.bind",
          "analysis.requirements.commit"
        ]),
        transitions: [
          { targetPhase: "query_planning", when: ({ actionName }) => actionName === "data.query.plan" },
          {
            targetPhase: "synthesis",
            when: ({ state }) => state.validationPassed && (state.currentEvidenceRefs ?? []).length > 0
          }
        ]
      },
      synthesis: {
        allowedActions: unique([
          ...commonActions,
          "inspect_schema",
          "semantic.context.resolve",
          "preview_table",
          "data.query.plan",
          "analysis.result.validate",
          "analysis.evidence.bind",
          "analysis.requirements.commit"
        ]),
        transitions: [{
          targetPhase: "query_planning",
          when: ({ actionName }) => actionName === "data.query.plan"
        }]
      }
    },
    createInitialState: () => ({
      schemaInspected: false,
      semanticResolved: false,
      semanticWarnings: [],
      contractGrounded: false,
      contractGroundingFindings: [],
      queryPlanned: false,
      queryValidated: false,
      currentQueryValidated: false,
      queryExecuted: false,
      validationPassed: false,
      currentEvidenceRefs: [],
      evidenceRefs: [],
      requirements: [...createCoreAnalysisRequirements(), ...cloneRequirements(userRequirements)],
      queryAttempts: [],
      evidenceBindings: [],
      reportedClaims: [],
      taskRequirementLinks: []
    }),
    completionPolicy: ({ contextPackageRef, state }) => {
      const requirementReasons = incompleteRequirementReasons(state);
      if (
        state.semanticResolved
        && state.contractGrounded
        && state.queryPlanned
        && state.currentQueryValidated
        && state.queryExecuted
        && state.validationPassed
        && (state.currentEvidenceRefs ?? []).length > 0
        && requirementReasons.length === 0
      ) {
        if (state.semanticMode === "fallback") {
          return {
            status: "degraded",
            evaluatedContextPackageRef: contextPackageRef,
            reasons: state.semanticWarnings.length > 0
              ? state.semanticWarnings
              : ["LOCAL_SEMANTIC_FALLBACK"],
            evidenceRefs: state.evidenceRefs
          };
        }
        return {
          status: "completed",
          evaluatedContextPackageRef: contextPackageRef,
          evidenceRefs: state.evidenceRefs
        };
      }
      const missing = [
        ...(state.schemaInspected ? [] : ["SCHEMA_GROUNDING_REQUIRED"]),
        ...(state.semanticResolved ? [] : ["SEMANTIC_GROUNDING_REQUIRED"]),
        ...(state.contractGrounded ? [] : ["ANALYSIS_CONTRACT_GROUNDING_REQUIRED"]),
        ...(state.queryPlanned ? [] : ["QUERY_PLAN_REQUIRED"]),
        ...(state.currentQueryValidated ? [] : ["QUERY_VALIDATION_REQUIRED"]),
        ...(state.queryExecuted ? [] : ["QUERY_EXECUTION_REQUIRED"]),
        ...(state.validationPassed ? [] : ["RESULT_VALIDATION_REQUIRED"]),
        ...((state.evidenceRefs ?? []).length > 0 ? [] : ["EVIDENCE_BINDING_REQUIRED"]),
        ...requirementReasons
      ];
      return { status: "continue", reasons: missing, allowedActions: allowedRecoveryActions(state) };
    }
  };
};

export const reduceDataAnalysisAction = (
  state: DataAnalysisState,
  actionName: string,
  result: unknown
): DataAnalysisState => {
  if (actionName === "inspect_schema") {
    const dialect = recordString(result, "dialect") ?? nestedString(result, "summary", "dialect");
    const schemaId = recordString(result, "schema_id") ?? recordString(result, "schemaId");
    return updateCoreRequirement({
      ...state,
      schemaInspected: true,
      contractGrounded: !hasUserRequirements(state) || state.contractGrounded,
      ...(schemaId ? { contractSchemaId: schemaId } : {}),
      ...(dialect ? { datasourceDialect: dialect } : {})
    }, "CORE_SCHEMA", "validated");
  }
  if (actionName === "semantic.context.resolve") {
    const semanticMode = recordString(result, "mode");
    const semanticTrust = recordString(result, "trust");
    const datasourceRevision = recordString(result, "datasourceRevision");
    const semanticContextChanged = state.contractGrounded && (
      (semanticMode !== undefined && state.semanticMode !== undefined && semanticMode !== state.semanticMode)
      || (semanticTrust !== undefined && state.semanticTrust !== undefined && semanticTrust !== state.semanticTrust)
      || (datasourceRevision !== undefined && state.contractDatasourceRevision !== undefined
        && datasourceRevision !== state.contractDatasourceRevision)
    );
    const next = {
      ...state,
      semanticResolved: semanticMode !== undefined && semanticMode !== "unavailable",
      contractGrounded: !hasUserRequirements(state) || (state.contractGrounded && !semanticContextChanged),
      ...(semanticMode ? { semanticMode } : {}),
      ...(semanticTrust ? { semanticTrust } : {}),
      semanticWarnings: recordStrings(result, "warnings")
    };
    return next.semanticResolved ? updateCoreRequirement(next, "CORE_SEMANTIC", "validated") : next;
  }
  if (actionName === "analysis.contract.ground") {
    const requirements = recordArray(result, "requirements").filter(isAnalysisRequirement);
    const datasourceRevision = recordString(result, "datasourceRevision");
    const schemaId = recordString(result, "schema_id") ?? recordString(result, "schemaId");
    return {
      ...state,
      contractGrounded: requirements.length > 0,
      ...(datasourceRevision ? { contractDatasourceRevision: datasourceRevision } : {}),
      ...(schemaId ? { contractSchemaId: schemaId } : {}),
      contractGroundingFindings: recordArray(result, "findings").map((finding) => ({
        ...(recordString(finding, "requirementId")
          ? { requirementId: recordString(finding, "requirementId") as string }
          : {}),
        ...(recordString(finding, "code") ? { code: recordString(finding, "code") as string } : {}),
        ...(recordString(finding, "message") ? { message: recordString(finding, "message") as string } : {})
      })),
      ...(requirements.length > 0 ? { requirements: cloneRequirements(requirements) } : {})
    };
  }
  if (actionName === "data.query.plan") {
    const assertionIds = recordStrings(result, "assertion_ids");
    const explicitRequirementIds = recordStrings(result, "requirement_ids");
    const requirementIds = explicitRequirementIds.length > 0
      ? explicitRequirementIds
      : deriveRequirementIdsFromAssertions(state, assertionIds);
    assertRequirementIds(state, requirementIds);
    const userRequirements = normalizedRequirements(state).filter((requirement) => requirement.source === "user");
    if (userRequirements.length > 0 && requirementIds.length === 0) {
      throw new Error("ANALYSIS_REQUIREMENT_IDS_REQUIRED");
    }
    const queryAttempts = normalizedQueryAttempts(state);
    const attemptId = `Q${queryAttempts.length + 1}`;
    const sql = recordString(result, "sql");
    const selectedRequirements = normalizedRequirements(state).filter((requirement) =>
      requirementIds.includes(requirement.id));
    const structuredAssertions = selectedRequirements.flatMap((requirement) => requirement.assertions ?? [])
      .filter((assertion) => assertion.kind !== "manual");
    if (structuredAssertions.length > 0 && assertionIds.length === 0) {
      throw new Error(`ANALYSIS_ASSERTION_IDS_REQUIRED:${requirementIds[0] ?? "unknown"}`);
    }
    const assertions = assertionIds.length > 0
      ? resolveRequirementAssertions(normalizedRequirements(state), requirementIds, assertionIds)
      : selectedRequirements.flatMap((requirement) => requirement.assertions ?? [])
        .filter((assertion) => assertion.kind === "manual");
    const attempt: AnalysisQueryAttempt = {
      id: attemptId,
      requirementIds,
      assertionIds,
      assertions,
      ...(sql ? { sql } : {}),
      expectedColumns: recordStrings(result, "expected_columns"),
      status: "planned",
      valid: false,
      resultFields: [],
      validationFindings: [],
      resultValidationFindings: [],
      verifiedValues: []
    };
    return updateRequirements({
      ...state,
      queryPlanned: true,
      currentQueryValidated: false,
      queryExecuted: false,
      validationPassed: false,
      currentEvidenceRefs: [],
      currentQueryAttemptId: attemptId,
      queryAttempts: [...queryAttempts, attempt]
    }, requirementIds, (requirement) => ({
      ...requirement,
      status: advanceStatus(requirement.status, "queried"),
      queryAttemptIds: unique([...requirement.queryAttemptIds, attemptId])
    }));
  }
  if (actionName === "data.query.validate") {
    const attempt = currentAttempt(state);
    const runtimeValid = recordBoolean(result, "valid") === true;
    const semanticFindings = runtimeValid && attempt?.sql
      && (attempt.assertions ?? []).some((assertion) => assertion.kind !== "manual")
      ? validateSqlSemantics(attempt.sql, state.datasourceDialect, attempt.assertions)
      : [];
    const runtimeFindings = recordStrings(result, "reasons").map((reason) => ({
      code: reason,
      message: reason,
      severity: "error" as const
    }));
    const validationFindings = [...runtimeFindings, ...semanticFindings];
    const valid = runtimeValid && !validationFindings.some((finding) => finding.severity === "error");
    let next = {
      ...state,
      queryValidated: state.queryValidated || valid,
      currentQueryValidated: valid,
      queryAttempts: updateCurrentAttempt(state, (attempt) => ({
        ...attempt,
        status: valid ? "validated" : "planned",
        valid,
        validationFindings
      }))
    };
    if (valid) {
      next = updateCoreRequirement(next, "CORE_QUERY", "validated");
    }
    return next;
  }
  if (actionName === "run_sql_readonly") {
    const artifactId = nestedArtifactId(result);
    const auditLogId = nestedString(result, "result", "audit_log_id");
    const resultFields = nestedStrings(result, "result", "columns");
    return {
      ...state,
      queryExecuted: true,
      validationPassed: false,
      currentEvidenceRefs: artifactId ? [artifactId] : [],
      evidenceRefs: artifactId ? unique([...(state.evidenceRefs ?? []), artifactId]) : state.evidenceRefs,
      queryAttempts: updateCurrentAttempt(state, (attempt) => ({
        ...attempt,
        status: "executed",
        ...(artifactId ? { artifactId } : {}),
        ...(auditLogId ? { auditLogId } : {}),
        resultFields
      }))
    };
  }
  if (actionName === "analysis.result.validate") {
    const valid = recordBoolean(result, "valid") === true;
    const resultValidationFindings = recordArray(result, "validation_findings")
      .filter(isAnalysisValidationFinding);
    const verifiedValues = recordArray(result, "verified_values").filter(isAnalysisVerifiedValue);
    let next = {
      ...state,
      validationPassed: valid,
      queryAttempts: updateCurrentAttempt(state, (attempt) => ({
        ...attempt,
        resultValidationFindings,
        verifiedValues
      }))
    };
    if (valid) {
      next = updateCoreRequirement(next, "CORE_RESULT", "validated");
      const requirementIds = currentAttempt(state)?.requirementIds ?? [];
      next = updateRequirements(next, requirementIds, (requirement) => ({
        ...requirement,
        status: advanceStatus(requirement.status, "validated")
      }));
    }
    return next;
  }
  if (actionName === "analysis.evidence.bind") {
    const evidenceRefs = recordStrings(result, "evidence_refs");
    const attempt = currentAttempt(state);
    const artifactId = recordString(result, "artifact_id") ?? attempt?.artifactId ?? evidenceRefs[0];
    const auditLogId = recordString(result, "audit_log_id") ?? attempt?.auditLogId;
    const resultFields = recordStrings(result, "result_fields");
    if ((attempt?.requirementIds.length ?? 0) > 0 && (!artifactId || !auditLogId || !state.validationPassed)) {
      throw new Error("ANALYSIS_EVIDENCE_BINDING_INCOMPLETE");
    }
    const bindings = createEvidenceBindings(state, attempt, artifactId, auditLogId, resultFields);
    let next = {
      ...state,
      currentEvidenceRefs: unique([...(state.currentEvidenceRefs ?? []), ...evidenceRefs]),
      evidenceRefs: unique([...(state.evidenceRefs ?? []), ...evidenceRefs]),
      evidenceBindings: [...normalizedEvidenceBindings(state), ...bindings],
      queryAttempts: updateCurrentAttempt(state, (queryAttempt) => ({ ...queryAttempt, status: "evidenced" }))
    };
    next = updateRequirements(next, attempt?.requirementIds ?? [], (requirement) => ({
      ...requirement,
      status: advanceStatus(requirement.status, "evidenced"),
      evidenceBindingIds: unique([
        ...requirement.evidenceBindingIds,
        ...bindings.filter((binding) => binding.requirementId === requirement.id).map((binding) => binding.id)
      ])
    }));
    return updateCoreRequirement(next, "CORE_EVIDENCE", "evidenced");
  }
  if (actionName === "analysis.requirements.commit") {
    return commitReportedClaims(state, result);
  }
  if (actionName === "task_write" || actionName === "task_update" || actionName === "task_complete") {
    return linkTasksToRequirements(state, result);
  }
  return state;
};

const allowedRecoveryActions = (state: DataAnalysisState): string[] => {
  if (!state.schemaInspected) return ["inspect_schema", "semantic.context.resolve"];
  if (!state.semanticResolved) return ["semantic.context.resolve"];
  if (!state.contractGrounded) return ["analysis.contract.ground"];
  if (!state.queryPlanned) return ["data.query.plan"];
  if (!state.currentQueryValidated) return ["data.query.validate"];
  if (!state.queryExecuted) {
    return ["inspect_schema", "preview_table", "data.query.plan", "data.query.validate", "run_sql_readonly"];
  }
  if (!state.validationPassed) return ["analysis.result.validate", "run_sql_readonly"];
  if ((state.currentEvidenceRefs ?? []).length === 0) return ["analysis.evidence.bind"];
  if (incompleteRequirementReasons(state).length > 0) return ["data.query.plan", "analysis.requirements.commit"];
  return [];
};

const hasUserRequirements = (state: DataAnalysisState): boolean =>
  normalizedRequirements(state).some((requirement) => requirement.source === "user");

const deriveRequirementIdsFromAssertions = (state: DataAnalysisState, assertionIds: string[]): string[] => unique(
  normalizedRequirements(state)
    .filter((requirement) => requirement.assertions.some((assertion) => assertionIds.includes(assertion.id)))
    .map((requirement) => requirement.id)
);

const isAnalysisRequirement = (value: unknown): value is AnalysisRequirement =>
  typeof value === "object"
  && value !== null
  && !Array.isArray(value)
  && typeof (value as Record<string, unknown>).id === "string"
  && Array.isArray((value as Record<string, unknown>).assertions);

const incompleteRequirementReasons = (state: DataAnalysisState): string[] => normalizedRequirements(state).flatMap(
  (requirement) => {
    if (!requirement.required) return [];
    if (requirement.source === "protocol") {
      return requirement.status === "pending" || requirement.status === "queried"
        ? [`ANALYSIS_CORE_REQUIREMENT_PENDING:${requirement.id}`]
        : [];
    }
    if (requirement.status === "reported") return [];
    return [requirement.status === "evidenced"
      ? `ANALYSIS_REQUIREMENT_NOT_REPORTED:${requirement.id}`
      : `ANALYSIS_REQUIREMENT_PENDING:${requirement.id}`];
  }
);

const updateCoreRequirement = (
  state: DataAnalysisState,
  requirementId: string,
  status: AnalysisRequirement["status"]
): DataAnalysisState => updateRequirements(state, [requirementId], (requirement) => ({
  ...requirement,
  status: advanceStatus(requirement.status, status)
}));

const updateRequirements = (
  state: DataAnalysisState,
  requirementIds: string[],
  update: (requirement: AnalysisRequirement) => AnalysisRequirement
): DataAnalysisState => {
  const idSet = new Set(requirementIds);
  return {
    ...state,
    requirements: normalizedRequirements(state).map((requirement) => idSet.has(requirement.id)
      ? update(requirement)
      : requirement)
  };
};

const assertRequirementIds = (state: DataAnalysisState, requirementIds: string[]): void => {
  const knownIds = new Set(normalizedRequirements(state).map((requirement) => requirement.id));
  for (const requirementId of requirementIds) {
    if (!knownIds.has(requirementId) || requirementId.startsWith("CORE_")) {
      throw new Error(`ANALYSIS_REQUIREMENT_NOT_FOUND:${requirementId}`);
    }
  }
};

const updateCurrentAttempt = (
  state: DataAnalysisState,
  update: (attempt: AnalysisQueryAttempt) => AnalysisQueryAttempt
): AnalysisQueryAttempt[] => normalizedQueryAttempts(state).map((attempt) =>
  attempt.id === state.currentQueryAttemptId ? update(attempt) : attempt);

const currentAttempt = (state: DataAnalysisState): AnalysisQueryAttempt | undefined =>
  normalizedQueryAttempts(state).find((attempt) => attempt.id === state.currentQueryAttemptId);

const createEvidenceBindings = (
  state: DataAnalysisState,
  attempt: AnalysisQueryAttempt | undefined,
  artifactId: string | undefined,
  auditLogId: string | undefined,
  resultFields: string[]
): AnalysisEvidenceBinding[] => {
  if (!attempt || !artifactId || !auditLogId || !state.validationPassed) return [];
  const offset = normalizedEvidenceBindings(state).length;
  return attempt.requirementIds.map((requirementId, index) => ({
    id: `E${offset + index + 1}`,
    requirementId,
    queryAttemptId: attempt.id,
    artifactId,
    auditLogId,
    resultFields: resultFields.length > 0 ? resultFields : attempt.resultFields,
    validationStatus: "passed"
  }));
};

const commitReportedClaims = (state: DataAnalysisState, result: unknown): DataAnalysisState => {
  const claims = recordArray(result, "claims");
  let next = { ...state, reportedClaims: [...normalizedReportedClaims(state)] };
  for (const value of claims) {
    const requirementId = recordString(value, "requirement_id");
    const claim = recordString(value, "claim");
    const evidenceBindingIds = recordStrings(value, "evidence_binding_ids");
    const evidenceRefs = recordStrings(value, "evidence_refs");
    const evidenceRequirementIds = recordStrings(value, "evidence_requirement_ids");
    if (!requirementId || !claim) throw new Error("ANALYSIS_REQUIREMENT_CLAIM_INVALID");
    assertRequirementIds(next, [requirementId]);
    assertRequirementIds(next, evidenceRequirementIds);
    const requirement = normalizedRequirements(next).find((candidate) => candidate.id === requirementId);
    const requirementBindings = normalizedEvidenceBindings(next).filter((binding) =>
      binding.requirementId === requirementId);
    const sourceBindings = normalizedEvidenceBindings(next).filter((binding) =>
      evidenceRequirementIds.includes(binding.requirementId));
    let candidateBindings = uniqueBindings([...requirementBindings, ...sourceBindings]);
    if (candidateBindings.length === 0 && (requirement?.kind === "validation" || requirement?.kind === "decision")) {
      candidateBindings = normalizedEvidenceBindings(next);
    }
    const hasExplicitEvidence = evidenceBindingIds.length > 0 || evidenceRefs.length > 0;
    let validBindings = hasExplicitEvidence
      ? candidateBindings.filter((binding) =>
          evidenceBindingIds.includes(binding.id) || evidenceRefs.includes(binding.artifactId))
      : candidateBindings;
    if (validBindings.length === 0 && candidateBindings.length > 0) {
      validBindings = candidateBindings;
    }
    if (validBindings.length === 0) {
      const invalidId = evidenceBindingIds[0];
      const invalidRef = evidenceRefs[0];
      throw new Error(
        `ANALYSIS_REQUIREMENT_EVIDENCE_INVALID:${requirementId}:${invalidId ?? invalidRef ?? "missing"}`
      );
    }
    const values = recordArray(value, "values").map(parseClaimValue);
    const boundAttemptIds = new Set(validBindings.map((binding) => binding.queryAttemptId));
    const boundAttempts = normalizedQueryAttempts(next).filter((attempt) => boundAttemptIds.has(attempt.id));
    const requirementAssertionIds = new Set(boundAttempts.flatMap((attempt) => attempt.assertions
      .filter((assertion) => assertion.requirementId === requirementId)
      .map((assertion) => assertion.id)));
    const verifiedValues = boundAttempts.flatMap((attempt) => attempt.verifiedValues ?? [])
      .filter((verifiedValue) => requirementAssertionIds.has(verifiedValue.assertionId));
    const requiredValueSpecs = (requirement?.assertions ?? []).flatMap((assertion) =>
      assertion.required && assertion.kind !== "manual"
        ? assertion.claimValues.filter((spec) => spec.required).map((spec) => ({
            assertionId: assertion.id,
            name: spec.name
          }))
        : []);
    for (const spec of requiredValueSpecs) {
      const verified = verifiedValues.some((value) =>
        value.assertionId === spec.assertionId && value.name === spec.name);
      if (!verified) {
        throw new Error(
          `ANALYSIS_CLAIM_VALUE_NOT_VERIFIED:${requirementId}:${spec.name}: execute and bind evidence for assertion `
          + `${spec.assertionId} before committing.`
        );
      }
    }
    const requiredValueNames = unique(requiredValueSpecs.map((spec) => spec.name));
    validateClaimValues(requirementId, values, verifiedValues, requiredValueNames);
    const claimId = `C${next.reportedClaims.length + 1}`;
    next.reportedClaims.push({
      id: claimId,
      requirementId,
      claim,
      evidenceBindingIds: validBindings.map((binding) => binding.id),
      values
    });
    next = updateRequirements(next, [requirementId], (requirement) => ({
      ...requirement,
      status: "reported",
      reportedClaimIds: unique([...requirement.reportedClaimIds, claimId])
    }));
  }
  return next;
};

const uniqueBindings = (bindings: AnalysisEvidenceBinding[]): AnalysisEvidenceBinding[] => [...new Map(
  bindings.map((binding) => [binding.id, binding])
).values()];

const linkTasksToRequirements = (state: DataAnalysisState, result: unknown): DataAnalysisState => {
  const tasks = recordArray(result, "tasks");
  let requirements = normalizedRequirements(state);
  const links = [...normalizedTaskLinks(state)];
  for (const task of tasks) {
    const taskId = recordString(task, "id");
    const content = recordString(task, "content");
    if (!taskId || !content) continue;
    const requirementIds = requirements
      .filter((requirement) => requirement.source === "user" && content.includes(requirement.id))
      .map((requirement) => requirement.id);
    if (requirementIds.length === 0) continue;
    const existing = links.find((link) => link.taskId === taskId);
    if (existing) {
      existing.requirementIds = unique([...existing.requirementIds, ...requirementIds]);
    } else {
      links.push({ taskId, requirementIds });
    }
    requirements = requirements.map((requirement) => requirementIds.includes(requirement.id)
      ? { ...requirement, taskIds: unique([...requirement.taskIds, taskId]) }
      : requirement);
  }
  return { ...state, requirements, taskRequirementLinks: links };
};

const advanceStatus = (
  current: AnalysisRequirement["status"],
  target: AnalysisRequirement["status"]
): AnalysisRequirement["status"] => {
  const order: AnalysisRequirement["status"][] = ["pending", "queried", "validated", "evidenced", "reported"];
  return order.indexOf(target) > order.indexOf(current) ? target : current;
};

const cloneRequirements = (requirements: AnalysisRequirement[]): AnalysisRequirement[] => requirements.map(
  (requirement) => ({
    ...requirement,
    acceptanceCriteria: [...requirement.acceptanceCriteria],
    assertions: cloneAssertions(requirement.assertions ?? []),
    taskIds: [...requirement.taskIds],
    queryAttemptIds: [...requirement.queryAttemptIds],
    evidenceBindingIds: [...requirement.evidenceBindingIds],
    reportedClaimIds: [...requirement.reportedClaimIds]
  })
);

const cloneAssertions = (assertions: AnalysisAssertion[]): AnalysisAssertion[] => assertions.map((assertion) => ({
  ...assertion,
  sourceTables: [...assertion.sourceTables],
  dimensions: [...assertion.dimensions],
  sqlConstraints: structuredClone(assertion.sqlConstraints),
  resultChecks: structuredClone(assertion.resultChecks),
  claimValues: structuredClone(assertion.claimValues)
}));

const normalizedRequirements = (state: DataAnalysisState): AnalysisRequirement[] => state.requirements ?? [];
const normalizedQueryAttempts = (state: DataAnalysisState): AnalysisQueryAttempt[] => state.queryAttempts ?? [];
const normalizedEvidenceBindings = (state: DataAnalysisState): AnalysisEvidenceBinding[] => state.evidenceBindings ?? [];
const normalizedReportedClaims = (state: DataAnalysisState): AnalysisReportedClaim[] => state.reportedClaims ?? [];
const normalizedTaskLinks = (state: DataAnalysisState): TaskRequirementLink[] => state.taskRequirementLinks ?? [];

const nestedArtifactId = (value: unknown): string | undefined =>
  recordString(value, "artifact_id") ?? recordString(recordValue(value, "result"), "artifact_id");

const nestedString = (value: unknown, parent: string, key: string): string | undefined =>
  recordString(recordValue(value, parent), key);

const nestedStrings = (value: unknown, parent: string, key: string): string[] =>
  recordStrings(recordValue(value, parent), key);

const recordValue = (value: unknown, key: string): unknown =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)[key]
    : undefined;

const recordString = (value: unknown, key: string): string | undefined => {
  const field = recordValue(value, key);
  return typeof field === "string" && field.length > 0 ? field : undefined;
};

const recordBoolean = (value: unknown, key: string): boolean | undefined => {
  const field = recordValue(value, key);
  return typeof field === "boolean" ? field : undefined;
};

const recordStrings = (value: unknown, key: string): string[] => {
  const field = recordValue(value, key);
  return Array.isArray(field) ? field.filter((item): item is string => typeof item === "string") : [];
};

const recordArray = (value: unknown, key: string): unknown[] => {
  const field = recordValue(value, key);
  return Array.isArray(field) ? field : [];
};

const parseClaimValue = (value: unknown): AnalysisClaimValue => {
  const name = recordString(value, "name");
  const scalar = recordValue(value, "value");
  const unit = recordString(value, "unit");
  if (!name || !isAnalysisScalar(scalar)) throw new Error("ANALYSIS_CLAIM_VALUE_INVALID");
  return { name, value: scalar, ...(unit ? { unit } : {}) };
};

const validateClaimValues = (
  requirementId: string,
  values: AnalysisClaimValue[],
  verifiedValues: AnalysisVerifiedValue[],
  requiredValueNames: string[]
): void => {
  const submittedNames = new Set<string>();
  for (const value of values) {
    if (submittedNames.has(value.name)) throw new Error(`ANALYSIS_CLAIM_VALUE_DUPLICATE:${value.name}`);
    submittedNames.add(value.name);
    const verified = verifiedValues.find((candidate) => candidate.name === value.name);
    if (!verified) {
      const allowedNames = unique(verifiedValues.map((candidate) => candidate.name)).join(", ") || "none";
      throw new Error(
        `ANALYSIS_CLAIM_VALUE_UNKNOWN:${requirementId}:${value.name}: use an exact verified name; `
        + `allowed names: ${allowedNames}.`
      );
    }
    const unitMatches = verified.unit === value.unit;
    if (!unitMatches || !claimScalarsEqual(value.value, verified.value, verified.tolerance)) {
      throw new Error(
        `ANALYSIS_CLAIM_VALUE_MISMATCH:${requirementId}:${value.name}: `
        + `expected ${formatVerifiedClaimValue(verified)}; received ${formatClaimValue(value)}.`
      );
    }
  }
  for (const name of requiredValueNames) {
    if (submittedNames.has(name)) continue;
    const verified = verifiedValues.find((candidate) => candidate.name === name);
    const expected = verified ? formatVerifiedClaimValue(verified) : "the verified value emitted by result validation";
    throw new Error(`ANALYSIS_CLAIM_VALUE_REQUIRED:${requirementId}:${name}: submit ${expected}.`);
  }
};

const formatVerifiedClaimValue = (value: AnalysisVerifiedValue): string =>
  `${formatClaimValue(value)}, tolerance=${value.tolerance}`;

const formatClaimValue = (value: AnalysisClaimValue | AnalysisVerifiedValue): string =>
  `value=${JSON.stringify(value.value)}, ${value.unit === undefined ? "no unit" : `unit=${JSON.stringify(value.unit)}`}`;

const claimScalarsEqual = (left: AnalysisScalar, right: AnalysisScalar, tolerance: number): boolean =>
  typeof left === "number" && typeof right === "number"
    ? Math.abs(left - right) <= tolerance
    : left === right;

const isAnalysisScalar = (value: unknown): value is AnalysisScalar =>
  value === null || ["string", "number", "boolean"].includes(typeof value);

const isAnalysisValidationFinding = (value: unknown): value is AnalysisQueryAttempt["validationFindings"][number] =>
  typeof value === "object" && value !== null && !Array.isArray(value)
  && typeof (value as Record<string, unknown>).code === "string"
  && typeof (value as Record<string, unknown>).message === "string"
  && ["error", "warning"].includes(String((value as Record<string, unknown>).severity));

const isAnalysisVerifiedValue = (value: unknown): value is AnalysisQueryAttempt["verifiedValues"][number] =>
  typeof value === "object" && value !== null && !Array.isArray(value)
  && typeof (value as Record<string, unknown>).name === "string"
  && typeof (value as Record<string, unknown>).assertionId === "string"
  && typeof (value as Record<string, unknown>).tolerance === "number"
  && Object.hasOwn(value, "value");

const unique = <T>(values: T[]): T[] => [...new Set(values)];
