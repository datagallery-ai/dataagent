import { z } from "zod";
import { ToolExecutionError } from "../errors/tool-execution-error.js";
import { AGENT_RUNTIME_LIMITS } from "../config/agent-runtime-limits.js";

import {
  ActionRouter,
  type ActionContextProjection,
  type ActionRouterOptions
} from "../capabilities/action-router.js";
import { CapabilityRegistry } from "../capabilities/capability-registry.js";
import { createToolCapabilityPlugin } from "../capabilities/tool-capability-plugin.js";
import type { CapabilityPlugin } from "../capabilities/types.js";
import type { AnalysisRequirementExtractor } from "./model-analysis-requirement-extractor.js";
import type {
  AnalysisContractGrounder,
  AnalysisContractGroundingInput
} from "./model-analysis-contract-grounder.js";
import type { AnalysisValidationFinding } from "./analysis-contract.js";
import { InMemoryProtocolStateStore } from "./in-memory-protocol-state-store.js";
import { ProtocolHandoffCoordinator } from "./protocol-handoff-coordinator.js";
import { ProtocolRegistry } from "./protocol-registry.js";
import {
  ProtocolRouter,
  type ProtocolClassifier,
  type ProtocolIdentity,
  type ProtocolRouteResult
} from "./protocol-router.js";
import { ProtocolRuntime, type ProtocolRuntimeOptions } from "./protocol-runtime.js";
import { verifyAnalysisResult } from "./result-verifier.js";
import {
  createDataAnalysisProtocol,
  reduceDataAnalysisAction,
  type DataAnalysisState
} from "./protocols/data-analysis.js";
import {
  createGeneralTaskProtocol,
  reduceGeneralTaskAction,
  type GeneralTaskState
} from "./protocols/general-task.js";
import type {
  AgentProtocolDefinition,
  ContextPackageRef,
  ProtocolGuardResult,
  ProtocolStateStore
} from "./types.js";
import type { SemanticRequest, SemanticResolution } from "../semantic/types.js";

type ExistingTool = { execute?: (...args: unknown[]) => unknown | Promise<unknown> };
type RunProtocolDomainState = GeneralTaskState | DataAnalysisState;

export type CreateRunProtocolBoundaryInput = {
  runId: string;
  userInput: string;
  authorizedProtocolIds: string[];
  initialContextPackageRef: ContextPackageRef;
  tools: Record<string, ExistingTool>;
  explicitProtocol?: ProtocolIdentity;
  classifier?: ProtocolClassifier;
  projectContext: ActionRouterOptions["projectContext"];
  serverPolicy?: ActionRouterOptions["serverPolicy"];
  resourceAuthorization?: ActionRouterOptions["resourceAuthorization"];
  runtimeOptions?: ProtocolRuntimeOptions;
  stateStore?: ProtocolStateStore;
  semanticProvider?: { resolve(request: SemanticRequest): Promise<SemanticResolution> };
  semanticRequest?: Omit<SemanticRequest, "query">;
  requirementExtractor?: AnalysisRequirementExtractor;
  analysisContractGrounder?: AnalysisContractGrounder;
  /** Compact routing context for follow-up intents. Lets the protocol classifier
   * see the previous round's query, protocol, and selected resources so a short
   * follow-up such as "再次尝试" can inherit the prior data-analysis intent. */
  routingContext?: RoutingContext;
};

export type RoutingContext = {
  /** The previous round's meaningful user query, if any. */
  previousQuery?: string;
  /** The previous round's resolved protocol and its terminal status, if any. */
  previousProtocol?: {
    protocolId: string;
    protocolVersion: string;
    /** Terminal completion status, e.g. completed/degraded/partial/failed. */
    terminalStatus?: string;
  };
  /** Skill IDs selected for this run. */
  selectedSkillIds?: string[];
  /** Datasource ID selected for this run. */
  selectedDatasourceId?: string;
};

export type RunProtocolBoundary = {
  actionRouter: ActionRouter<RunProtocolDomainState>;
  capabilityRegistry: CapabilityRegistry;
  protocolRuntime: ProtocolRuntime<RunProtocolDomainState>;
  handoffCoordinator: ProtocolHandoffCoordinator;
  route: ProtocolRouteResult;
  segmentId: string;
  acknowledgeEvent(event: import("./types.js").ProtocolEvent): void;
  dispose(): Promise<void>;
};

/** Resolve a formal protocol and bind every selected tool to its governed action boundary. */
export const createRunProtocolBoundary = async (
  input: CreateRunProtocolBoundaryInput
): Promise<RunProtocolBoundary> => {
  const actionNames = Object.keys(input.tools);
  const stateStore = input.stateStore ?? new InMemoryProtocolStateStore();
  const persistedState = stateStore.find<RunProtocolDomainState>(input.runId);
  if (persistedState && !input.authorizedProtocolIds.includes(persistedState.protocolId)) {
    throw new Error(`PROTOCOL_NOT_AUTHORIZED:${persistedState.protocolId}@${persistedState.protocolVersion}`);
  }
  if (
    persistedState
    && input.explicitProtocol
    && (input.explicitProtocol.protocolId !== persistedState.protocolId
      || input.explicitProtocol.protocolVersion !== persistedState.protocolVersion)
  ) {
    throw new Error("PROTOCOL_RESUME_SELECTION_MISMATCH");
  }
  const shouldExtractRequirements = !persistedState
    && Boolean(input.requirementExtractor)
    && (input.explicitProtocol?.protocolId === "data-analysis" || analyticIntent(input.userInput));
  const userRequirements = shouldExtractRequirements
    ? await input.requirementExtractor?.({ userText: input.userInput }) ?? []
    : [];
  const protocolRegistry = new ProtocolRegistry();
  protocolRegistry.register(createGeneralTaskProtocol(actionNames));
  protocolRegistry.register(createDataAnalysisProtocol(actionNames, userRequirements));
  const router = new ProtocolRouter(protocolRegistry, {
    ...(input.classifier ? { classifier: input.classifier } : {})
  });
  let route: ProtocolRouteResult;
  try {
    route = await router.route({
      authorizedProtocolIds: input.authorizedProtocolIds,
      ...(!persistedState && input.explicitProtocol ? { explicit: input.explicitProtocol } : {}),
      deterministicCandidates: persistedState
        ? [{
            protocolId: persistedState.protocolId,
            protocolVersion: persistedState.protocolVersion,
            priority: 1000,
            reasonCode: "PROTOCOL_SEGMENT_RESTORED"
          }]
        : analyticIntent(input.userInput)
        ? [{
            protocolId: "data-analysis",
            protocolVersion: "1",
            priority: 100,
            reasonCode: "ANALYTIC_INTENT"
          }]
        : [],
      classificationInput: buildClassificationInput(input.userInput, input.routingContext)
    });
  } catch (error) {
    input.runtimeOptions?.onEvent?.({
      eventId: `${input.runId}:segment:1:0:protocol.route.failed`,
      type: "protocol.route.failed",
      runId: input.runId,
      segmentId: `${input.runId}:segment:1`,
      protocolId: "unresolved",
      protocolVersion: "0",
      revision: 0,
      payload: { reason: error instanceof Error ? error.message : String(error) }
    });
    throw error;
  }
  // Consistency guard (issue: "a run cannot expose a data-analysis tool policy
  // while being governed by a protocol that rejects every data action"). When the
  // resolved protocol is general-task but the run exposes data tools and a selected
  // datasource, emit a warning so the agent can self-correct via protocol_handoff.
  if (route.definition.id === "general-task" && exposesDataToolPolicy(input) && input.routingContext?.selectedDatasourceId) {
    const warningCode = "PROTOCOL_TOOL_POLICY_MISMATCH";
    route = {
      ...route,
      warnings: [...route.warnings, warningCode]
    };
    input.runtimeOptions?.onEvent?.({
      eventId: `${input.runId}:segment:1:0:protocol.route.consistency.warning`,
      type: "protocol.route.consistency.warning",
      runId: input.runId,
      segmentId: `${input.runId}:segment:1`,
      protocolId: route.definition.id,
      protocolVersion: route.definition.version,
      revision: 0,
      payload: { code: warningCode, reason: "general-task rejects every data action while data tools are exposed" }
    });
  }
  let activeProtocolId = route.definition.id;
  const reduceAction = (state: unknown, actionName: string, result: unknown): unknown =>
    activeProtocolId === "data-analysis"
      ? reduceDataAnalysisAction(state as DataAnalysisState, actionName, result)
      : reduceGeneralTaskAction(state as GeneralTaskState, actionName, result);
  const capabilityRegistry = new CapabilityRegistry();
  capabilityRegistry.register(createToolCapabilityPlugin({
    id: "selected-run-tools",
    tools: input.tools,
    reduceAction
  }));
  capabilityRegistry.register(createRuntimeActionPlugin(
    reduceAction,
    input.semanticProvider,
    input.analysisContractGrounder
  ));
  await capabilityRegistry.initialize();
  let segmentId = persistedState?.segmentId ?? `${input.runId}:segment:1`;
  const runtimeOptions: ProtocolRuntimeOptions = {
    ...(input.runtimeOptions ?? {}),
    maxActions: input.runtimeOptions?.maxActions ?? (route.definition.id === "data-analysis"
      ? AGENT_RUNTIME_LIMITS.dataAnalysisMaxProtocolActions
      : AGENT_RUNTIME_LIMITS.generalTaskMaxProtocolActions),
    ...(!persistedState
      ? {
          startEvents: [
            {
              type: "protocol.route.requested",
              payload: {
                authorizedProtocolIds: input.authorizedProtocolIds,
                explicit: input.explicitProtocol
              }
            },
            ...(route.source === "classifier"
              ? [{
                  type: "protocol.route.classified",
                  payload: { reasonCodes: route.reasonCodes, warnings: route.warnings }
                }]
              : []),
            {
              type: "protocol.route.resolved",
              payload: {
                protocolId: route.definition.id,
                protocolVersion: route.definition.version,
                reasonCodes: route.reasonCodes,
                source: route.source,
                warnings: route.warnings
              }
            },
            ...(route.definition.id === "data-analysis" && userRequirements.length > 0
              ? [{
                  type: "analysis.requirements.extracted",
                  payload: {
                    requirements: userRequirements.map((requirement) => ({
                      id: requirement.id,
                      kind: requirement.kind,
                      description: requirement.description,
                      required: requirement.required,
                      assertions: requirement.assertions.map((assertion) => ({
                        id: assertion.id,
                        kind: assertion.kind,
                        required: assertion.required
                      }))
                    }))
                  }
                }]
              : [])
          ]
        }
      : {})
  };
  let protocolRuntime = new ProtocolRuntime(
    route.definition as AgentProtocolDefinition<RunProtocolDomainState>,
    stateStore,
    runtimeOptions
  );
  if (persistedState) {
    protocolRuntime.restore(input.runId, segmentId);
  } else {
    protocolRuntime.start({
      runId: input.runId,
      segmentId,
      contextPackageRef: input.initialContextPackageRef
    });
  }
  const handoffCoordinator = new ProtocolHandoffCoordinator(protocolRegistry, stateStore, {
    ...(runtimeOptions.onEvent ? { onEvent: runtimeOptions.onEvent } : {})
  });
  const actionRouter = new ActionRouter(capabilityRegistry, protocolRuntime, {
    automaticActions: (actionInput) => activeProtocolId === "data-analysis"
      ? dataAnalysisAutomaticActions(actionInput, input)
      : [],
    preparatoryActions: (actionInput) => activeProtocolId === "data-analysis"
      ? dataAnalysisPreparatoryActions(actionInput)
      : [],
    afterPreparatoryActions: ({ actionName, domain, input: actionInput, phase }) => {
      if (activeProtocolId !== "data-analysis") {
        return;
      }
      const dataAnalysisState = domain as DataAnalysisState;
      if (actionName === "run_sql_readonly") {
        assertCurrentQueryContract(dataAnalysisState);
      }
      if (isReportFileAction(actionName, actionInput, phase)) {
        assertRequirementsCommittedBeforeReport(dataAnalysisState);
      }
    },
    serverPolicy: input.serverPolicy ?? allowAction,
    ...(input.resourceAuthorization ? { resourceAuthorization: input.resourceAuthorization } : {}),
    projectContext: input.projectContext,
    projectFinalObservation: ({ actionName, domain, observation }) =>
      activeProtocolId === "data-analysis" && actionName === "inspect_schema"
        ? projectGroundedSchemaObservation(observation, domain as DataAnalysisState)
        : observation,
    projectProtocolEventResult: ({ actionName, rawResult }) => {
      if (actionName === "semantic.context.resolve") {
        return semanticResolutionEventResult(rawResult);
      }
      return actionName === "analysis.contract.ground"
        ? analysisContractGroundingEventResult(rawResult)
        : undefined;
    },
    afterAction: ({ actionName, rawResult }) => {
      if (actionName !== "protocol.handoff.propose") {
        return;
      }
      const targetProtocolId = directString(rawResult, "targetProtocolId");
      const targetProtocolVersion = directString(rawResult, "targetProtocolVersion");
      if (!targetProtocolId || !targetProtocolVersion) {
        throw new Error("PROTOCOL_HANDOFF_PROPOSAL_INVALID");
      }
      const current = protocolRuntime.getState(input.runId, segmentId);
      const handoff = handoffCoordinator.handoff({
        runId: input.runId,
        segmentId,
        expectedRevision: current.revision,
        authorizedProtocolIds: input.authorizedProtocolIds,
        target: { protocolId: targetProtocolId, protocolVersion: targetProtocolVersion },
        reasonCodes: recordStringArray(rawResult, "reasonCodes"),
        unresolvedGoals: recordStringArray(rawResult, "unresolvedGoals")
      });
      const targetDefinition = protocolRegistry.find(targetProtocolId, targetProtocolVersion);
      if (!targetDefinition) {
        throw new Error("PROTOCOL_HANDOFF_TARGET_UNAVAILABLE");
      }
      activeProtocolId = targetProtocolId;
      segmentId = handoff.next.segmentId;
      protocolRuntime = new ProtocolRuntime(
        targetDefinition as AgentProtocolDefinition<RunProtocolDomainState>,
        stateStore,
        { ...runtimeOptions, startEvents: [] }
      );
      protocolRuntime.restore(input.runId, segmentId);
      actionRouter.replaceProtocolRuntime(protocolRuntime);
    }
  });
  return {
    actionRouter,
    capabilityRegistry,
    handoffCoordinator,
    route,
    get protocolRuntime() {
      return protocolRuntime;
    },
    get segmentId() {
      return segmentId;
    },
    acknowledgeEvent: (event) => stateStore.acknowledgeEvent(event),
    dispose: () => capabilityRegistry.dispose()
  };
};

const semanticResolutionEventResult = (value: unknown): Record<string, unknown> => {
  const provider = directString(value, "provider");
  const mode = directString(value, "mode");
  const trust = directString(value, "trust");
  const datasourceRevision = directString(value, "datasourceRevision");
  const fallbackReason = directString(value, "fallbackReason");
  return {
    ...(provider ? { provider } : {}),
    ...(mode ? { mode } : {}),
    ...(trust ? { trust } : {}),
    ...(datasourceRevision ? { datasourceRevision } : {}),
    ...(fallbackReason ? { fallbackReason } : {})
  };
};

const analysisContractGroundingEventResult = (value: unknown): Record<string, unknown> => {
  const requirements = recordArray(value, "requirements").filter((requirement) =>
    directString(requirement, "source") === "user");
  const structuredRequirementIds: string[] = [];
  const manualRequirementIds: string[] = [];
  for (const requirement of requirements) {
    const requirementId = directString(requirement, "id");
    if (!requirementId) {
      continue;
    }
    const hasStructuredAssertion = recordArray(requirement, "assertions").some((assertion) =>
      directString(assertion, "kind") !== "manual");
    (hasStructuredAssertion ? structuredRequirementIds : manualRequirementIds).push(requirementId);
  }
  return {
    ...(directString(value, "datasourceRevision")
      ? { datasourceRevision: directString(value, "datasourceRevision") }
      : {}),
    structuredRequirementIds,
    manualRequirementIds,
    findings: recordArray(value, "findings").map((finding) => ({
      ...(directString(finding, "requirementId")
        ? { requirementId: directString(finding, "requirementId") }
        : {}),
      ...(directString(finding, "code") ? { code: directString(finding, "code") } : {}),
      ...(directString(finding, "message") ? { message: directString(finding, "message") } : {})
    }))
  };
};

const projectGroundedSchemaObservation = (
  observation: unknown,
  state: DataAnalysisState
): Record<string, unknown> => {
  const schemaObservation = typeof observation === "object" && observation !== null && !Array.isArray(observation)
    ? observation as Record<string, unknown>
    : { schema: observation };
  return {
    ...schemaObservation,
    analysis_contract: {
      instruction: [
        "Use the exact requirement_id, assertion_id, aggregate aliases, and expected columns below in",
        "run_sql_readonly. Do not invent or rename contract fields."
      ].join(" "),
      requirements: state.requirements
        .filter((requirement) => requirement.source === "user")
        .map((requirement) => ({
          requirement_id: requirement.id,
          description: requirement.description,
          acceptance_criteria: [...requirement.acceptanceCriteria],
          assertions: requirement.assertions.map((assertion) => ({
            assertion_id: assertion.id,
            kind: assertion.kind,
            description: assertion.description,
            source_tables: [...assertion.sourceTables],
            dimensions: [...assertion.dimensions],
            sql_constraints: structuredClone(assertion.sqlConstraints),
            result_checks: structuredClone(assertion.resultChecks),
            claim_values: structuredClone(assertion.claimValues)
          }))
        }))
    }
  };
};

const createRuntimeActionPlugin = (
  reduceAction: (state: unknown, actionName: string, result: unknown) => unknown,
  semanticProvider?: { resolve(request: SemanticRequest): Promise<SemanticResolution> },
  analysisContractGrounder?: AnalysisContractGrounder
): CapabilityPlugin => {
  const names = [
    "general.answer.commit",
    "protocol.handoff.propose",
    "semantic.context.resolve",
    "analysis.contract.ground",
    "data.query.plan",
    "data.query.validate",
    "analysis.result.validate",
    "analysis.evidence.bind",
    "analysis.requirements.commit"
  ];
  return {
    manifest: { id: "protocol-runtime-actions", version: "1", provides: names },
    actions: names.map((name) => ({
      name,
      exposure: name === "protocol.handoff.propose" || name === "analysis.requirements.commit" ? "agent" : "runtime",
      inputSchema: z.unknown(),
      outputSchema: z.unknown(),
      idempotency: "supported",
      execute: async (_context, actionInput) => executeRuntimeAction(
        name,
        actionInput,
        semanticProvider,
        analysisContractGrounder
      ),
      reduce: (state, result) => reduceAction(state, name, result)
    }))
  };
};

const executeRuntimeAction = async (
  name: string,
  actionInput: unknown,
  semanticProvider?: { resolve(request: SemanticRequest): Promise<SemanticResolution> },
  analysisContractGrounder?: AnalysisContractGrounder
): Promise<unknown> => {
  if (name === "semantic.context.resolve" && semanticProvider) {
    return semanticProvider.resolve(actionInput as SemanticRequest);
  }
  if (name === "analysis.contract.ground") {
    const groundingInput = actionInput as AnalysisContractGroundingInput;
    const result = analysisContractGrounder
      ? await analysisContractGrounder(groundingInput)
      : { requirements: groundingInput.requirements, findings: [] };
    return {
      ...result,
      datasourceRevision: groundingInput.datasourceRevision,
      schema_id: directString(groundingInput.physicalSchema, "schema_id")
        ?? directString(groundingInput.physicalSchema, "schemaId")
    };
  }
  if (name === "data.query.validate") {
    const sql = directString(actionInput, "sql");
    const schemaId = directString(actionInput, "schema_id") ?? directString(actionInput, "schemaId");
    const sqlReasons = validateReadonlySql(sql);
    return {
      valid: Boolean(sql && schemaId && sqlReasons.length === 0),
      reasons: [
        ...(sql ? [] : ["SQL_REQUIRED"]),
        ...(schemaId ? [] : ["SCHEMA_ID_REQUIRED"]),
        ...sqlReasons
      ]
    };
  }
  return actionInput;
};

const validateReadonlySql = (sql: string | undefined): string[] => {
  if (!sql) {
    return [];
  }
  const normalized = stripLeadingSqlComments(sql).trim().replace(/;\s*$/u, "");
  const reasons: string[] = [];
  if (!/^(?:select|with)\b/iu.test(normalized)) {
    reasons.push("SQL_NOT_READ_ONLY");
  }
  if (
    /\b(?:insert|update|delete|drop|alter|create|grant|revoke|copy|call|pragma|attach|detach|vacuum|truncate|merge)\b/iu
      .test(normalized)
  ) {
    reasons.push("SQL_MUTATION_KEYWORD_FORBIDDEN");
  }
  if (normalized.includes(";")) {
    reasons.push("SQL_MULTIPLE_STATEMENTS_FORBIDDEN");
  }
  return reasons;
};

const stripLeadingSqlComments = (sql: string): string => {
  let remaining = sql.trimStart();
  while (remaining.startsWith("--") || remaining.startsWith("/*")) {
    if (remaining.startsWith("--")) {
      const lineEnd = remaining.indexOf("\n");
      remaining = lineEnd < 0 ? "" : remaining.slice(lineEnd + 1).trimStart();
      continue;
    }
    const blockEnd = remaining.indexOf("*/", 2);
    remaining = blockEnd < 0 ? "" : remaining.slice(blockEnd + 2).trimStart();
  }
  return remaining;
};

const analyticIntent = (userInput: string): boolean =>
  /\b(?:sql|query|metric|analytics?|statistics?)\b|分析|统计|指标|数据|销售额/iu.test(userInput);

const DATA_TOOL_NAMES = new Set(["list_data_sources", "inspect_schema", "preview_table", "run_sql_readonly"]);

/** Whether the run exposes any data-analysis tool in its selected tool set. */
const exposesDataToolPolicy = (input: CreateRunProtocolBoundaryInput): boolean =>
  Object.keys(input.tools).some((name) => DATA_TOOL_NAMES.has(name));

/** Build the compact routing context the classifier sees alongside the current query. */
const buildClassificationInput = (
  userInput: string,
  routingContext?: RoutingContext
): Record<string, unknown> => {
  const value: Record<string, unknown> = { userText: userInput };
  if (!routingContext) {
    return value;
  }
  if (routingContext.previousQuery) {
    value.previousQuery = routingContext.previousQuery;
  }
  if (routingContext.previousProtocol) {
    value.previousProtocol = {
      protocolId: routingContext.previousProtocol.protocolId,
      protocolVersion: routingContext.previousProtocol.protocolVersion,
      ...(routingContext.previousProtocol.terminalStatus
        ? { terminalStatus: routingContext.previousProtocol.terminalStatus }
        : {})
    };
  }
  if (routingContext.selectedSkillIds?.length) {
    value.selectedSkillIds = [...routingContext.selectedSkillIds];
  }
  if (routingContext.selectedDatasourceId) {
    value.selectedDatasourceId = routingContext.selectedDatasourceId;
  }
  return value;
};

const allowAction = (): ProtocolGuardResult => ({ allowed: true });

const dataAnalysisPreparatoryActions = (input: {
  actionName: string;
  input: unknown;
}): Array<{ actionName: string; input: unknown }> => input.actionName === "run_sql_readonly"
  ? [
      { actionName: "data.query.plan", input: input.input },
      { actionName: "data.query.validate", input: input.input }
    ]
  : [];

const assertCurrentQueryContract = (state: DataAnalysisState): void => {
  if (state.currentQueryValidated) {
    return;
  }
  const attempt = state.queryAttempts.find((candidate) => candidate.id === state.currentQueryAttemptId)
    ?? state.queryAttempts.at(-1);
  const findings = attempt?.validationFindings ?? [];
  const findingCodes = findings.map((finding) => finding.code).join(", ") || "QUERY_CONTRACT_INVALID";
  const exactCorrections = findings.map((finding) => finding.message).join(" ")
    || "The query does not satisfy its selected analysis assertions.";
  throw new ToolExecutionError({
    ok: false,
    isError: true,
    error: {
      code: "QUERY_CONTRACT_VALIDATION_FAILED",
      category: "validation",
      message: `SQL was not executed because contract validation failed: ${findingCodes}. ${exactCorrections}`,
      executionStatus: "not_started",
      retryable: false,
      details: {
        queryAttemptId: attempt?.id ?? "unknown",
        findings: structuredClone(findings),
        allowedActions: ["data.query.plan", "data.query.validate", "inspect_schema", "preview_table"]
      }
    },
    recovery: {
      strategy: "refresh_and_replan",
      instruction: `Apply these exact SQL corrections, then submit a new query plan: ${exactCorrections}`,
      avoid: ["Do not repeat the same invalid SQL without addressing the listed findings."]
    }
  });
};

const assertRequirementsCommittedBeforeReport = (state: DataAnalysisState): void => {
  const incomplete = state.requirements.filter((requirement) =>
    requirement.source === "user" && requirement.required && requirement.status !== "reported");
  if (incomplete.length === 0) {
    return;
  }
  throw new ToolExecutionError({
    ok: false,
    isError: true,
    error: {
      code: "ANALYSIS_REQUIREMENTS_COMMIT_REQUIRED",
      category: "validation",
      message: "The final analysis output cannot be written until every required analysis claim is committed.",
      executionStatus: "not_started",
      retryable: false,
      details: {
        requirementIds: incomplete.map((requirement) => requirement.id),
        requirements: incomplete.map((requirement) => ({
          id: requirement.id,
          status: requirement.status,
          recovery: requirement.status === "evidenced"
            ? "Commit this claim with analysis_requirements_commit."
            : "Finish validated SQL evidence before committing this claim."
        }))
      }
    },
    recovery: {
      strategy: "refresh_and_replan",
      instruction: "Commit evidenced claims, finish any still-pending analyses, then write the final output.",
      avoid: ["Do not retry the final report write while required claims remain unreported."]
    }
  });
};

const isReportFileAction = (actionName: string, input: unknown, phase: string): boolean => {
  if (actionName !== "write_file" && actionName !== "edit_file") {
    return false;
  }
  if (phase === "synthesis") {
    return true;
  }
  const filePath = directString(input, "path") ?? directString(input, "filename") ?? "";
  return /\.(?:html?|markdown|md|rst|txt)$/iu.test(filePath.trim().replace(/\/+$/u, ""));
};

const dataAnalysisAutomaticActions = (input: {
  actionName: string;
  domain: unknown;
  input: unknown;
  rawResult: unknown;
}, boundaryInput: CreateRunProtocolBoundaryInput): Array<{ actionName: string; input: unknown }> => {
  if (input.actionName === "inspect_schema" && boundaryInput.semanticProvider && boundaryInput.semanticRequest) {
    return [{
      actionName: "semantic.context.resolve",
      input: {
        ...boundaryInput.semanticRequest,
        query: boundaryInput.userInput,
        physicalSchema: input.rawResult
      }
    }];
  }
  if (input.actionName === "semantic.context.resolve") {
    const state = input.domain as DataAnalysisState;
    const userRequirements = state.requirements.filter((requirement) => requirement.source === "user");
    if (userRequirements.length === 0 || state.contractGrounded) {
      return [];
    }
    return [{
      actionName: "analysis.contract.ground",
      input: {
        requirements: state.requirements,
        physicalSchema: recordValue(input.input, "physicalSchema"),
        semanticResolution: input.rawResult,
        datasourceRevision: directString(input.input, "datasourceRevision") ?? "unknown"
      }
    }];
  }
  if (input.actionName !== "run_sql_readonly") {
    return [];
  }
  const artifactId = nestedString(input.rawResult, "result", "artifact_id")
    ?? directString(input.rawResult, "artifact_id");
  const auditLogId = nestedString(input.rawResult, "result", "audit_log_id")
    ?? directString(input.rawResult, "audit_log_id");
  const resultFields = nestedStringArray(input.rawResult, "result", "columns");
  const validation = validateAnalysisResult(input.rawResult, input.input, input.domain as DataAnalysisState);
  return [
    { actionName: "analysis.result.validate", input: validation },
    ...(artifactId && validation.valid
      ? [{
          actionName: "analysis.evidence.bind",
          input: {
            artifact_id: artifactId,
            ...(auditLogId ? { audit_log_id: auditLogId } : {}),
            evidence_refs: [artifactId],
            result_fields: resultFields
          }
        }]
      : [])
  ];
};

const validateAnalysisResult = (
  value: unknown,
  actionInput: unknown,
  state: DataAnalysisState
): {
  valid: boolean;
  reasons: string[];
  validation_findings: AnalysisValidationFinding[];
  verified_values: unknown[];
} => {
  const result = recordValue(value, "result");
  const columns = recordValue(result, "columns");
  const rows = recordValue(result, "rows");
  const rowCount = recordValue(result, "row_count");
  const auditLogId = directString(result, "audit_log_id");
  const expectedColumns = recordStringArray(actionInput, "expected_columns");
  const missingColumns = expectedColumns.filter((column) => !Array.isArray(columns) || !columns.includes(column));
  const reasons = [
    ...(Array.isArray(columns) ? [] : ["RESULT_COLUMNS_REQUIRED"]),
    ...(Array.isArray(rows) ? [] : ["RESULT_ROWS_REQUIRED"]),
    ...(typeof rowCount === "number" && rowCount >= 0 ? [] : ["RESULT_ROW_COUNT_REQUIRED"]),
    ...(auditLogId ? [] : ["RESULT_AUDIT_LOG_REQUIRED"]),
    ...missingColumns.map((column) => `RESULT_EXPECTED_COLUMN_MISSING:${column}`)
  ];
  const structuralFindings: AnalysisValidationFinding[] = reasons.map((reason) => ({
    code: reason,
    message: `Result contract failed: ${reason}.`,
    severity: "error"
  }));
  const attempt = state.queryAttempts?.find((candidate) => candidate.id === state.currentQueryAttemptId);
  const verification = Array.isArray(columns) && Array.isArray(rows) && typeof rowCount === "number"
    ? verifyAnalysisResult({
        columns: columns.filter((column): column is string => typeof column === "string"),
        rows,
        rowCount
      }, attempt?.assertions ?? [])
    : { valid: false, findings: [], verifiedValues: [] };
  const validationFindings = [...structuralFindings, ...verification.findings];
  return {
    valid: validationFindings.every((finding) => finding.severity !== "error"),
    reasons: validationFindings.map((finding) => finding.code),
    validation_findings: validationFindings,
    verified_values: verification.verifiedValues
  };
};

const nestedString = (value: unknown, parent: string, key: string): string | undefined =>
  directString(recordValue(value, parent), key);

const nestedStringArray = (value: unknown, parent: string, key: string): string[] =>
  recordStringArray(recordValue(value, parent), key);

const directString = (value: unknown, key: string): string | undefined => {
  const field = recordValue(value, key);
  return typeof field === "string" && field.length > 0 ? field : undefined;
};

const recordStringArray = (value: unknown, key: string): string[] => {
  const field = recordValue(value, key);
  return Array.isArray(field)
    ? field.filter((item): item is string => typeof item === "string" && item.length > 0)
    : [];
};

const recordArray = (value: unknown, key: string): unknown[] => {
  const field = recordValue(value, key);
  return Array.isArray(field) ? field : [];
};

const recordValue = (value: unknown, key: string): unknown =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)[key]
    : undefined;

export type RunProtocolContextProjection = ContextPackageRef | ActionContextProjection;
