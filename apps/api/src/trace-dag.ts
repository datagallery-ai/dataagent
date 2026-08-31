import type {
  ArtifactRecord,
  CheckpointRecord,
  ConversationMessageRecord,
  MetadataStore,
  RunEventRecord,
  RunRecord,
  SessionBranchRecord,
  TraceSectionRecord
} from "@datafoundry/metadata";

import {
  listVisibleConversationMessages,
  resolveSessionLineage,
  type SessionLineage
} from "./session-branching.js";

export type TraceDagNodeKind =
  | "artifact"
  | "branch"
  | "context"
  | "run-start"
  | "run-terminal"
  | "tool"
  | "user-turn";

export type TraceDagContextDetail = {
  type: "context";
  assistantOutput?: string | undefined;
  budgetTokens?: number | undefined;
  decisions?: unknown[] | undefined;
  inputBudget?: number | undefined;
  model?: string | undefined;
  modelProfileId?: string | undefined;
  omittedGroupIds?: string[] | undefined;
  omittedSources?: unknown[] | undefined;
  packageId?: string | undefined;
  packageRevision?: number | undefined;
  planId?: string | undefined;
  promptTokens?: number | undefined;
  reasoning?: string | undefined;
  remainingTokens?: number | undefined;
  selectedGroupIds?: string[] | undefined;
  selectedSources?: unknown[] | undefined;
  stepNumber?: number | undefined;
  tokenReport?: unknown | undefined;
  totalTokens?: number | undefined;
};

export type TraceDagToolDetail = {
  type: "tool";
  arguments?: unknown | undefined;
  argumentsText?: string | undefined;
  result?: unknown | undefined;
  resultText?: string | undefined;
  toolName?: string | undefined;
};

export type TraceDagArtifactDetail = {
  type: "artifact";
  artifactType?: string | undefined;
  mimeType?: string | undefined;
  name?: string | undefined;
  preview?: unknown | undefined;
};

export type TraceDagTerminalDetail = {
  type: "terminal";
  error?: string | undefined;
  message?: string | undefined;
};

export type TraceDagNodeDetail =
  | TraceDagArtifactDetail
  | TraceDagContextDetail
  | TraceDagTerminalDetail
  | TraceDagToolDetail;

export type TraceDagNode = {
  id: string;
  kind: TraceDagNodeKind;
  label: string;
  artifactId?: string | undefined;
  checkpointId?: string | undefined;
  checkpointKind?: CheckpointRecord["kind"] | undefined;
  checkpointStatus?: CheckpointRecord["status"] | undefined;
  createdAt?: string | undefined;
  eventSeq?: number | undefined;
  messageId?: string | undefined;
  messagePosition?: number | undefined;
  prominent?: boolean | undefined;
  rollbackable?: boolean | undefined;
  runId?: string | undefined;
  sessionId?: string | undefined;
  status?: string | undefined;
  summary?: string | undefined;
  toolCallId?: string | undefined;
  detail?: TraceDagNodeDetail | undefined;
};

export type TraceDagEdgeKind =
  | "branches_from"
  | "continues_to"
  | "emits"
  | "produces_artifact"
  | "starts_run";

export type TraceDagEdge = {
  id: string;
  source: string;
  target: string;
  kind: TraceDagEdgeKind;
  label?: string | undefined;
};

export type TraceDagDto = {
  sessionId: string;
  nodes: TraceDagNode[];
  edges: TraceDagEdge[];
  sections: TraceDagSection[];
};

export type TraceDagSection = {
  id: string;
  runId: string;
  phaseKey: string;
  status: TraceSectionRecord["status"];
  title: string;
  summary: string;
  startEventSeq: number;
  endEventSeq: number;
  nodeIds: string[];
};

type AddEventNodeResult = {
  activeContextNodeId?: string | undefined;
  lastNodeId?: string | undefined;
};

export function buildSessionTraceDag(input: {
  limit?: number;
  metadataStore: MetadataStore;
  sessionId: string;
  userId: string;
}): TraceDagDto {
  const lineage = resolveSessionLineage({
    metadataStore: input.metadataStore,
    sessionId: input.sessionId,
    userId: input.userId
  });
  const messages = listVisibleConversationMessages({
    lineage,
    limit: input.limit ?? 200,
    metadataStore: input.metadataStore,
    userId: input.userId
  });
  const runIds = visibleRunIds(messages, lineage.branch);
  const runById = new Map(
    runIds.flatMap((runId) => {
      const run = input.metadataStore.runs.find({ user_id: input.userId, run_id: runId });
      return run ? [[runId, run] as const] : [];
    })
  );
  const eventsByRun = new Map(
    runIds.map((runId) => [
      runId,
      input.metadataStore.runEvents.listByRun({ user_id: input.userId, run_id: runId })
    ])
  );
  const checkpoints = visibleContextCheckpoints({
    lineage,
    metadataStore: input.metadataStore,
    runIds,
    userId: input.userId
  });
  const artifactsByRun = new Map(
    runIds.map((runId) => [
      runId,
      input.metadataStore.artifacts.listByRun({ user_id: input.userId, run_id: runId })
    ])
  );

  const builder = new TraceDagBuilder(runById, checkpoints);
  const userNodeByRunId = addUserTurnNodes(builder, messages);
  const runLastNodeByRunId = new Map<string, string>();

  for (const runId of runIds) {
    const run = runById.get(runId);
    const runStartNodeId = `run:${runId}:start`;
    builder.addNode({
      id: runStartNodeId,
      kind: "run-start",
      label: "Run started",
      createdAt: run?.started_at,
      runId,
      sessionId: run?.session_id,
      status: run?.status,
      summary: run?.user_input ? truncate(run.user_input, 220) : undefined
    });
    const userNodeId = userNodeByRunId.get(runId);
    if (userNodeId) {
      builder.addEdge(userNodeId, runStartNodeId, "starts_run");
    }

    let lastNodeId = runStartNodeId;
    let activeContextNodeId: string | undefined;
    const toolNodeIds = new Map<string, string>();
    for (const eventRecord of eventsByRun.get(runId) ?? []) {
      const result = addEventNode(builder, {
        activeContextNodeId,
        eventRecord,
        lastNodeId,
        toolNodeIds
      });
      if (result?.activeContextNodeId) {
        activeContextNodeId = result.activeContextNodeId;
      }
      if (result?.lastNodeId) {
        lastNodeId = result.lastNodeId;
      }
    }
    runLastNodeByRunId.set(runId, lastNodeId);
  }

  for (const [runId, artifacts] of artifactsByRun) {
    addArtifactNodes(builder, artifacts, runLastNodeByRunId.get(runId));
  }

  addCheckpointBranchNodes(builder, {
    branches: input.metadataStore.sessionBranches.listChildrenForParents({
      user_id: input.userId,
      parent_session_ids: lineage.segments.map((segment) => segment.sessionId)
    }),
    metadataStore: input.metadataStore,
    userId: input.userId
  });

  const nodes = builder.nodes();

  return {
    sessionId: input.sessionId,
    nodes,
    edges: builder.edges(),
    sections: visibleTraceSections({
      lineage,
      metadataStore: input.metadataStore,
      nodes,
      runIds,
      userId: input.userId
    })
  };
}

class TraceDagBuilder {
  private readonly checkpointByEvent = new Map<string, CheckpointRecord[]>();
  private readonly checkpointByTool = new Map<string, CheckpointRecord>();
  private readonly checkpointNodeId = new Map<string, string>();
  private readonly edgeById = new Map<string, TraceDagEdge>();
  private readonly nodeById = new Map<string, TraceDagNode>();

  constructor(
    private readonly runById: Map<string, RunRecord>,
    checkpoints: CheckpointRecord[]
  ) {
    for (const checkpoint of checkpoints) {
      const eventKey = checkpointEventKey(checkpoint.run_id, checkpoint.event_seq);
      const current = this.checkpointByEvent.get(eventKey) ?? [];
      current.push(checkpoint);
      this.checkpointByEvent.set(eventKey, current);
      if (checkpoint.tool_call_id) {
        this.checkpointByTool.set(checkpointToolKey(checkpoint.run_id, checkpoint.tool_call_id), checkpoint);
      }
    }
  }

  addNode(node: TraceDagNode): TraceDagNode {
    const existing = this.nodeById.get(node.id);
    const next = existing ? mergeTraceNode(existing, node) : { ...node };
    const checkpoint = this.resolveCheckpoint(next);
    if (checkpoint) {
      this.attachCheckpoint(next, checkpoint);
    }
    this.nodeById.set(node.id, next);
    return next;
  }

  addEdge(source: string | undefined, target: string | undefined, kind: TraceDagEdgeKind, label?: string): void {
    if (!source || !target || source === target) {
      return;
    }
    const id = `${source}->${target}:${kind}`;
    if (this.edgeById.has(id)) {
      return;
    }
    this.edgeById.set(id, {
      id,
      source,
      target,
      kind,
      ...(label ? { label } : {})
    });
  }

  checkpointNode(checkpointId: string): string | undefined {
    return this.checkpointNodeId.get(checkpointId);
  }

  edges(): TraceDagEdge[] {
    return [...this.edgeById.values()];
  }

  nodes(): TraceDagNode[] {
    return [...this.nodeById.values()];
  }

  private attachCheckpoint(node: TraceDagNode, checkpoint: CheckpointRecord): void {
    const run = this.runById.get(checkpoint.run_id);
    node.checkpointId = checkpoint.id;
    node.checkpointKind = checkpoint.kind;
    node.checkpointStatus = checkpoint.status;
    node.rollbackable = Boolean(run && isEndedRun(run) && checkpoint.status !== "failed");
    this.checkpointNodeId.set(checkpoint.id, node.id);
  }

  private resolveCheckpoint(node: TraceDagNode): CheckpointRecord | undefined {
    if (!node.runId) {
      return undefined;
    }
    if (node.toolCallId) {
      const toolCheckpoint = this.checkpointByTool.get(checkpointToolKey(node.runId, node.toolCallId));
      if (toolCheckpoint) {
        return toolCheckpoint;
      }
    }
    if (node.eventSeq === undefined) {
      return undefined;
    }
    const checkpoints = this.checkpointByEvent.get(checkpointEventKey(node.runId, node.eventSeq)) ?? [];
    return preferredCheckpointForNode(node, checkpoints);
  }
}

function mergeTraceNode(existing: TraceDagNode, node: TraceDagNode): TraceDagNode {
  const incoming = preserveToolLabel(existing, node);
  const next = { ...existing, ...incoming };
  if (existing.kind === "context" && incoming.kind === "context" && incoming.label === "Context compiled") {
    next.label = existing.label;
  }
  if (existing.summary && !incoming.summary) {
    next.summary = existing.summary;
  }
  if (existing.detail || incoming.detail) {
    next.detail = mergeTraceNodeDetail(existing.detail, incoming.detail);
  }
  return next;
}

function mergeTraceNodeDetail(
  existing: TraceDagNodeDetail | undefined,
  incoming: TraceDagNodeDetail | undefined
): TraceDagNodeDetail | undefined {
  if (!existing) {
    return incoming;
  }
  if (!incoming || existing.type !== incoming.type) {
    return existing;
  }
  if (existing.type === "context" && incoming.type === "context") {
    return {
      ...existing,
      ...incoming,
      assistantOutput: appendText(existing.assistantOutput, incoming.assistantOutput),
      reasoning: appendText(existing.reasoning, incoming.reasoning)
    };
  }
  if (existing.type === "tool" && incoming.type === "tool") {
    const argumentsText = appendText(existing.argumentsText, incoming.argumentsText);
    return {
      ...existing,
      ...incoming,
      arguments: incoming.arguments ?? parseMaybeJson(argumentsText) ?? existing.arguments,
      argumentsText,
      result: incoming.result ?? existing.result,
      resultText: incoming.resultText ?? existing.resultText,
      toolName: incoming.toolName ?? existing.toolName
    };
  }
  return { ...existing, ...incoming };
}

function preserveToolLabel(existing: TraceDagNode, next: TraceDagNode): TraceDagNode {
  if (existing.kind !== "tool" || next.kind !== "tool" || next.label !== "Tool call") {
    return next;
  }
  return existing.label === "Tool call" ? next : { ...next, label: existing.label };
}

function addUserTurnNodes(
  builder: TraceDagBuilder,
  messages: ConversationMessageRecord[]
): Map<string, string> {
  const userNodeByRunId = new Map<string, string>();
  messages.forEach((message, index) => {
    if (message.role !== "user") {
      return;
    }
    const nodeId = `message:${message.id}`;
    builder.addNode({
      id: nodeId,
      kind: "user-turn",
      label: `User turn ${index + 1}`,
      createdAt: message.created_at,
      messageId: message.message_id ?? message.id,
      messagePosition: index + 1,
      prominent: true,
      runId: message.run_id,
      sessionId: message.session_id,
      summary: truncate(message.content_text, 220)
    });
    if (!userNodeByRunId.has(message.run_id)) {
      userNodeByRunId.set(message.run_id, nodeId);
    }
  });
  return userNodeByRunId;
}

function addEventNode(
  builder: TraceDagBuilder,
  input: {
    activeContextNodeId: string | undefined;
    eventRecord: RunEventRecord;
    lastNodeId: string;
    toolNodeIds: Map<string, string>;
  }
): AddEventNodeResult | undefined {
  const event = parseRecord(input.eventRecord.payload_json);
  const type = stringValue(event.type);
  if (!type) {
    return undefined;
  }

  if (type === "CUSTOM" && stringValue(event.name) === "context.compiled") {
    const nodeId = `run:${input.eventRecord.run_id}:event:${input.eventRecord.seq}:context`;
    builder.addNode({
      id: nodeId,
      kind: "context",
      label: contextCompiledLabel(event.value),
      createdAt: input.eventRecord.created_at,
      eventSeq: input.eventRecord.seq,
      runId: input.eventRecord.run_id,
      sessionId: input.eventRecord.session_id,
      summary: contextCompiledSummary(event.value),
      detail: contextCompiledDetail(event.value)
    });
    builder.addEdge(input.lastNodeId, nodeId, "emits", "compiled context");
    return { activeContextNodeId: nodeId, lastNodeId: nodeId };
  }

  if (type === "CUSTOM" && stringValue(event.name) === "context.prompt-verified") {
    updateActiveContext(builder, input, contextPromptVerifiedDetail(event.value));
    return undefined;
  }

  if (type === "REASONING_MESSAGE_CONTENT") {
    updateActiveContext(builder, input, { type: "context", reasoning: stringValue(event.delta) });
    return undefined;
  }

  if (type === "TEXT_MESSAGE_CHUNK" && stringValue(event.role) === "assistant") {
    updateActiveContext(builder, input, { type: "context", assistantOutput: stringValue(event.delta) });
    return undefined;
  }

  if (
    type === "TOOL_CALL_ARGS" ||
    type === "TOOL_CALL_END" ||
    type === "TOOL_CALL_RESULT" ||
    type === "TOOL_CALL_START"
  ) {
    return addToolEventNode(builder, input, event, type);
  }

  if (type === "RUN_FINISHED" || type === "RUN_ERROR") {
    const nodeId = `run:${input.eventRecord.run_id}:event:${input.eventRecord.seq}:terminal`;
    builder.addNode({
      id: nodeId,
      kind: "run-terminal",
      label: type === "RUN_ERROR" ? "Run failed" : "Run completed",
      createdAt: input.eventRecord.created_at,
      eventSeq: input.eventRecord.seq,
      runId: input.eventRecord.run_id,
      sessionId: input.eventRecord.session_id,
      status: type === "RUN_ERROR" ? "failed" : "completed",
      summary: stringValue(event.message) ?? stringValue(event.error),
      detail: {
        type: "terminal",
        error: stringValue(event.error),
        message: stringValue(event.message)
      }
    });
    builder.addEdge(input.lastNodeId, nodeId, "emits", "finished");
    return { lastNodeId: nodeId };
  }

  return undefined;
}

function updateActiveContext(
  builder: TraceDagBuilder,
  input: { activeContextNodeId: string | undefined; eventRecord: RunEventRecord },
  detail: TraceDagContextDetail | undefined
): void {
  if (!input.activeContextNodeId || !detail) {
    return;
  }
  builder.addNode({
    id: input.activeContextNodeId,
    kind: "context",
    label: "Context compiled",
    runId: input.eventRecord.run_id,
    sessionId: input.eventRecord.session_id,
    detail
  });
}

function addToolEventNode(
  builder: TraceDagBuilder,
  input: {
    activeContextNodeId: string | undefined;
    eventRecord: RunEventRecord;
    lastNodeId: string;
    toolNodeIds: Map<string, string>;
  },
  event: Record<string, unknown>,
  type: string
): AddEventNodeResult | undefined {
  const toolCallId = stringValue(event.toolCallId) ?? stringValue(event.tool_call_id);
  if (!toolCallId) {
    return undefined;
  }
  const nodeId = input.toolNodeIds.get(toolCallId) ?? `run:${input.eventRecord.run_id}:tool:${toolCallId}`;
  const toolName = stringValue(event.toolCallName) ?? stringValue(event.toolName) ?? stringValue(event.tool_name);
  const status = type === "TOOL_CALL_RESULT"
    ? toolStatusFromResult(event.content)
    : type === "TOOL_CALL_END"
      ? "completed"
      : "running";
  builder.addNode({
    id: nodeId,
    kind: "tool",
    label: toolName ? `Tool: ${toolName}` : "Tool call",
    createdAt: input.eventRecord.created_at,
    eventSeq: input.eventRecord.seq,
    runId: input.eventRecord.run_id,
    sessionId: input.eventRecord.session_id,
    status,
    summary: toolSummary(event, type),
    detail: toolDetail(event, type, toolName),
    toolCallId
  });
  if (!input.toolNodeIds.has(toolCallId)) {
    builder.addEdge(input.lastNodeId, nodeId, "emits", "tool");
  }
  input.toolNodeIds.set(toolCallId, nodeId);
  return { lastNodeId: nodeId };
}

function addArtifactNodes(
  builder: TraceDagBuilder,
  artifacts: ArtifactRecord[],
  parentNodeId: string | undefined
): void {
  for (const artifact of artifacts) {
    const nodeId = `artifact:${artifact.id}`;
    builder.addNode({
      id: nodeId,
      kind: "artifact",
      label: artifact.name,
      artifactId: artifact.id,
      createdAt: artifact.created_at,
      runId: artifact.run_id,
      sessionId: artifact.session_id,
      status: artifact.type,
      summary: artifact.mime_type,
      detail: {
        type: "artifact",
        artifactType: artifact.type,
        mimeType: artifact.mime_type,
        name: artifact.name,
        preview: parseMaybeJson(artifact.preview_json)
      }
    });
    builder.addEdge(parentNodeId, nodeId, "produces_artifact", "produces");
  }
}

function addCheckpointBranchNodes(
  builder: TraceDagBuilder,
  input: {
    branches: SessionBranchRecord[];
    metadataStore: MetadataStore;
    userId: string;
  }
): void {
  for (const branch of input.branches) {
    if (!branch.fork_checkpoint_id) {
      continue;
    }
    const checkpointNodeId = builder.checkpointNode(branch.fork_checkpoint_id);
    if (!checkpointNodeId) {
      continue;
    }
    const branchNodeId = `branch:${branch.id}`;
    builder.addNode({
      id: branchNodeId,
      kind: "branch",
      label: safeSessionTitle(input.metadataStore, input.userId, branch.child_session_id) || "Checkpoint branch",
      createdAt: branch.created_at,
      runId: branch.fork_run_id,
      sessionId: branch.child_session_id,
      summary: branch.fork_checkpoint_id
    });
    builder.addEdge(checkpointNodeId, branchNodeId, "branches_from", "branches");
  }
}

function safeSessionTitle(metadataStore: MetadataStore, userId: string, sessionId: string): string | undefined {
  try {
    return metadataStore.sessions.get({ user_id: userId, session_id: sessionId }).title;
  } catch {
    return undefined;
  }
}

function visibleRunIds(messages: ConversationMessageRecord[], branch?: SessionBranchRecord): string[] {
  return uniqueStrings([
    ...messages.map((message) => message.run_id),
    ...(branch?.fork_checkpoint_id ? [branch.fork_run_id] : [])
  ]);
}

function visibleContextCheckpoints(input: {
  lineage: SessionLineage;
  metadataStore: MetadataStore;
  runIds: string[];
  userId: string;
}): CheckpointRecord[] {
  const runIdSet = new Set(input.runIds);
  return input.lineage.segments.flatMap((segment) =>
    input.metadataStore.checkpoints
      .listBySession({ user_id: input.userId, session_id: segment.sessionId, limit: 500 })
      .filter((checkpoint) => runIdSet.has(checkpoint.run_id))
  );
}

function visibleTraceSections(input: {
  lineage: SessionLineage;
  metadataStore: MetadataStore;
  nodes: TraceDagNode[];
  runIds: string[];
  userId: string;
}): TraceDagSection[] {
  const visibleRunIds = new Set(input.runIds);
  const nodeIdsByRun = new Map<string, TraceDagNode[]>();
  for (const node of input.nodes) {
    if (!node.runId || node.eventSeq === undefined) {
      continue;
    }
    const nodes = nodeIdsByRun.get(node.runId) ?? [];
    nodes.push(node);
    nodeIdsByRun.set(node.runId, nodes);
  }
  return input.metadataStore.traceSections
    .listBySessions({
      user_id: input.userId,
      session_ids: input.lineage.segments.map((segment) => segment.sessionId)
    })
    .filter((section) => visibleRunIds.has(section.run_id))
    .map((section) => ({
      id: section.id,
      runId: section.run_id,
      phaseKey: section.phase_key,
      status: section.status,
      title: section.title,
      summary: section.summary,
      startEventSeq: section.start_event_seq,
      endEventSeq: section.end_event_seq,
      nodeIds: (nodeIdsByRun.get(section.run_id) ?? [])
        .filter((node) => node.eventSeq! >= section.start_event_seq && node.eventSeq! <= section.end_event_seq)
        .map((node) => node.id)
    }));
}

function preferredCheckpointForNode(
  node: TraceDagNode,
  checkpoints: CheckpointRecord[]
): CheckpointRecord | undefined {
  if (node.kind === "context") {
    return checkpoints.find((checkpoint) => checkpoint.kind === "context-compiled") ?? checkpoints[0];
  }
  if (node.kind === "run-terminal") {
    return checkpoints.find((checkpoint) => checkpoint.kind === "run-terminal") ?? checkpoints[0];
  }
  return checkpoints[0];
}

function isEndedRun(run: RunRecord): boolean {
  return run.status === "completed" || run.status === "failed" || run.status === "canceled";
}

function checkpointEventKey(runId: string, eventSeq: number): string {
  return `${runId}:${eventSeq}`;
}

function checkpointToolKey(runId: string, toolCallId: string): string {
  return `${runId}:${toolCallId}`;
}

function contextCompiledLabel(value: unknown): string {
  const record = recordValue(value);
  const stepNumber = numberValue(record?.step_number);
  return stepNumber === undefined ? "Context compiled" : `Context step ${stepNumber}`;
}

function contextCompiledSummary(value: unknown): string | undefined {
  const record = recordValue(value);
  const packageId = stringValue(record?.package_id);
  const revision = numberValue(record?.package_revision);
  if (!packageId || revision === undefined) {
    return undefined;
  }
  return `${packageId} rev ${revision}`;
}

function contextCompiledDetail(value: unknown): TraceDagContextDetail {
  const record = recordValue(value);
  return {
    type: "context",
    budgetTokens: numberValue(record?.budget_tokens),
    decisions: arrayValue(record?.decisions),
    inputBudget: numberValue(record?.inputBudget) ?? numberValue(record?.input_budget),
    model: stringValue(record?.model),
    omittedGroupIds: stringArrayValue(record?.omitted_group_ids),
    omittedSources: arrayValue(record?.omitted_sources),
    packageId: stringValue(record?.package_id),
    packageRevision: numberValue(record?.package_revision),
    planId: stringValue(record?.plan_id),
    promptTokens: numberValue(record?.prompt_tokens),
    remainingTokens: numberValue(record?.remaining_tokens),
    selectedGroupIds: stringArrayValue(record?.selected_group_ids),
    selectedSources: arrayValue(record?.selected_sources),
    stepNumber: numberValue(record?.step_number),
    tokenReport: record?.token_report,
    totalTokens: numberValue(record?.total_tokens)
  };
}

function contextPromptVerifiedDetail(value: unknown): TraceDagContextDetail | undefined {
  const record = recordValue(value);
  if (!record) {
    return undefined;
  }
  return {
    type: "context",
    budgetTokens: numberValue(record.budget_tokens),
    inputBudget: numberValue(record.input_budget),
    model: stringValue(record.model),
    modelProfileId: stringValue(record.model_profile_id),
    promptTokens: numberValue(record.prompt_tokens),
    remainingTokens: numberValue(record.remaining_tokens),
    stepNumber: numberValue(record.step_number),
    totalTokens: numberValue(record.total_tokens)
  };
}

function toolSummary(event: Record<string, unknown>, type: string): string | undefined {
  if (type === "TOOL_CALL_RESULT") {
    return truncate(valuePreview(event.content), 220);
  }
  return stringValue(event.delta) ?? stringValue(event.argsText) ?? stringValue(event.input);
}

function toolDetail(
  event: Record<string, unknown>,
  type: string,
  toolName: string | undefined
): TraceDagToolDetail {
  const argumentsText = type === "TOOL_CALL_ARGS"
    ? stringValue(event.delta) ?? stringValue(event.argsText) ?? stringValue(event.input)
    : undefined;
  const resultText = type === "TOOL_CALL_RESULT" ? valuePreview(event.content) : undefined;
  return {
    type: "tool",
    arguments: parseMaybeJson(argumentsText),
    argumentsText,
    result: parseMaybeJson(resultText),
    resultText,
    toolName
  };
}

function toolStatusFromResult(value: unknown): string {
  const text = valuePreview(value).trim();
  const parsed = parseMaybeJson(text);
  const record = recordValue(parsed);
  const status = stringValue(record?.status)?.toLowerCase();
  if (status === "error" || status === "failed" || status === "failure") {
    return "failed";
  }
  if (record?.success === false || stringValue(record?.error)) {
    return "failed";
  }
  return /^(error|failed|failure)\b/iu.test(text) ? "failed" : "completed";
}

function valuePreview(value: unknown): string {
  if (value === undefined) {
    return "";
  }
  return typeof value === "string" ? value : JSON.stringify(value) ?? "";
}

function truncate(value: string | undefined, maxLength: number): string | undefined {
  if (!value) {
    return undefined;
  }
  return value.length > maxLength ? `${value.slice(0, maxLength)}...` : value;
}

function parseRecord(value: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(value) as unknown;
    return recordValue(parsed) ?? {};
  } catch {
    return {};
  }
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : undefined;
}

function arrayValue(value: unknown): unknown[] | undefined {
  return Array.isArray(value) ? value : undefined;
}

function stringArrayValue(value: unknown): string[] | undefined {
  const array = arrayValue(value);
  if (!array) {
    return undefined;
  }
  return array.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function appendText(existing: string | undefined, incoming: string | undefined): string | undefined {
  if (!incoming) {
    return existing;
  }
  return existing ? `${existing}${incoming}` : incoming;
}

function parseMaybeJson(value: string | undefined): unknown | undefined {
  if (!value) {
    return undefined;
  }
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return undefined;
  }
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}
