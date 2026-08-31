import {
  MastraContextBudgetProcessor,
  ContextPackageBuilder,
  ContextPromptMaterializer,
  ContextRunState,
  MastraContextRuntimeSourceProcessor,
  createToolObservationBoundary,
  createDefaultContextSourcePolicy,
  createMastraContextProcessorBoundary,
  MastraConversationContextAdapter,
  LongTermMemoryContextSource,
  ModelContextProfileRegistry,
  MastraProviderPromptGuardProcessor,
  ReductionStrategyRegistry,
  RetrieveKnowledgeToolObservationAdapter,
  RuntimeContextSourceRegistry,
  SchemaToolObservationAdapter,
  SqlResultToolObservationAdapter,
  ContextStepPlanner,
  ToolObservationDispatcher,
  WorkingMemoryProjectionContextSource,
  createDefaultRuntimeContextSourceRegistry,
  createDataFoundry,
  createContextItem,
  createContextSourceMetadata,
  contextItemDedupeKeys,
  contextItemExclusivityKey,
  contextItemOverlapKeys,
  contextItemSourceKind,
  contextItemSourceOwner,
  groupMessagesByTurn
} from "../packages/agent-runtime/dist/testing.js";
import { convertAGUIMessagesToMastra } from "@ag-ui/mastra";
import { MessageList } from "@mastra/core/agent";

const identity = { resourceId: "context-smoke-user", sessionId: "context-smoke-session", runId: "context-smoke-run" };
const runState = new ContextRunState(identity);
const builder = new ContextPackageBuilder();
const sourcePolicy = createDefaultContextSourcePolicy();
const packageOne = builder.build([
  createContextItem({
    id: "artifact-ref-1",
    sourceType: "sql",
    sourceId: "sql-1",
    groupId: "sql-observation-1",
    visibility: "artifact-ref",
    trust: "tool",
    retention: "reference",
    priority: 10,
    content: { artifact_id: "artifact-1", source: "sql" },
    metadata: { groupKind: "reference" }
  })
], identityToPackageOptions(identity));

runState.merge(packageOne);
runState.merge(packageOne);

if (runState.package.revision !== 2 || runState.package.items.length !== 1) {
  throw new Error("Expected package revisions to increment without duplicating stable context items");
}
if (runState.package.artifactRefs[0]?.artifact_id !== "artifact-1") {
  throw new Error("Expected artifact references to survive package merges");
}

runState.registerPackage(packageOne);
runState.registerPackage(builder.build(packageOne.items, identityToPackageOptions(identity)));
const registeredPackageItems = runState.package.items.filter((item) => item.id.endsWith(":artifact-ref-1"));

if (registeredPackageItems.length !== 2 || new Set(registeredPackageItems.map((item) => item.id)).size !== 2) {
  throw new Error("Expected separate registered packages with local item IDs to remain distinct");
}

const toolObservationBoundary = createToolObservationBoundary({
  identity: { ...identity, runId: "context-tool-observation-history-run" }
});
const toolObservationState = toolObservationBoundary.contextRunState;
const toolObservationPackager = toolObservationBoundary.packager;
const returnedToolPackage = toolObservationPackager.packageToolObservation({
  toolName: "run_sql_readonly",
  rawResult: {
    result: {
      columns: ["id"],
      rows: [[1]],
      row_count: 1,
      audit_log_id: "audit-context-smoke",
      elapsed_ms: 3
    },
    sql: "select 1 as id"
  },
  runScope: {
    modelName: "context-smoke-model",
    resourceId: identity.resourceId,
    runId: toolObservationState.identity.runId,
    sessionId: identity.sessionId
  }
});
const returnedToolModelItem = returnedToolPackage.items.find((item) => item.visibility === "model");
if (!returnedToolModelItem || returnedToolModelItem.retention !== "supporting") {
  throw new Error("Expected direct tool observation package to remain model-visible for the live tool result");
}
const registeredToolItem = toolObservationState.package.items.find((item) => item.id.endsWith(":sql-model"));
if (registeredToolItem?.visibility !== "reference" || registeredToolItem.retention !== "reference") {
  throw new Error("Expected registered tool observation history to be retained as reference inventory");
}
if (registeredToolItem.metadata.groupKind !== "reference") {
  throw new Error("Expected registered tool observation history to use reference inventory grouping");
}
const registeredToolPromptGroups = new ContextPromptMaterializer().createGroups({
  contextPackage: toolObservationState.package
});
if (registeredToolPromptGroups.some((group) => group.id.includes("sql-observation"))) {
  throw new Error("Expected registered tool observation history to stay out of prompt planning groups");
}

const knowledgeObservationBoundary = createToolObservationBoundary({
  identity: { ...identity, runId: "context-knowledge-tool-history-run" },
  includeKnowledge: true
});
const knowledgePackage = knowledgeObservationBoundary.packager.packageToolObservation({
  toolName: "retrieve_knowledge",
  rawResult: {
    collection_id: "knowledge-history",
    chunks: [{
      chunk_id: "knowledge-history-chunk",
      content: "Knowledge tool result should only be live in the tool result message.",
      document_id: "knowledge-history-doc",
      score: 1
    }]
  },
  runScope: {
    modelName: "context-smoke-model",
    resourceId: identity.resourceId,
    runId: "context-knowledge-tool-history-run",
    sessionId: identity.sessionId
  }
});
if (!knowledgePackage.items.some((item) => item.visibility === "model" && item.metadata.groupKind === "source")) {
  throw new Error("Expected direct Knowledge tool package to expose a live model source result");
}
const registeredKnowledgeItems = knowledgeObservationBoundary.contextRunState.package.items.filter((item) =>
  item.sourceType === "knowledge-retrieval"
);
if (
  registeredKnowledgeItems.length === 0 ||
  registeredKnowledgeItems.some((item) => item.visibility === "model" || item.metadata.groupKind === "source")
) {
  throw new Error("Expected registered Knowledge tool history to be downgraded to reference inventory");
}
const registeredKnowledgePromptGroups = new ContextPromptMaterializer().createGroups({
  contextPackage: knowledgeObservationBoundary.contextRunState.package
});
if (registeredKnowledgePromptGroups.some((group) => group.id.includes("knowledge-retrieval-observation"))) {
  throw new Error("Expected registered Knowledge tool history to stay out of source prompt groups");
}

const anonymousMessages = [
  createAnonymousMessage("user", "anonymous question"),
  createAnonymousMessage("assistant", "anonymous answer"),
  createAnonymousMessage("user", "current anonymous question"),
  createToolCallMessage(),
  createAnonymousToolResultMessage()
];
const anonymousGroups = groupMessagesByTurn(anonymousMessages);

if (anonymousGroups.length !== 2 || anonymousGroups[0]?.mandatory || !anonymousGroups[1]?.mandatory) {
  throw new Error("Expected only the current anonymous conversation turn to be mandatory");
}
if (anonymousGroups[1]?.members.length !== 3) {
  throw new Error("Expected the current user, tool call, and tool result to remain in one turn");
}

const duplicateAnonymousGroups = groupMessagesByTurn([
  createAnonymousMessage("user", "same content"),
  createAnonymousMessage("user", "same content")
]);
const duplicateAnonymousIds = duplicateAnonymousGroups.map((group) => group.members[0]?.id);

if (new Set(duplicateAnonymousIds).size !== 2) {
  throw new Error("Expected identical anonymous messages to receive distinct occurrence identities");
}

const originalStableId = groupMessagesByTurn([createAnonymousMessage("user", "stable question")])[0]?.members[0]?.id;
const prependedStableId = groupMessagesByTurn([
  createAnonymousMessage("assistant", "unrelated prefix"),
  createAnonymousMessage("user", "stable question")
])[1]?.members[0]?.id;

if (!originalStableId || originalStableId !== prependedStableId) {
  throw new Error("Expected anonymous identity to be independent from its absolute message index");
}

const orphanGroup = groupMessagesByTurn([
  createAnonymousMessage("assistant", "orphan assistant"),
  createAnonymousToolResultMessage()
])[0];

if (!orphanGroup?.mandatory || orphanGroup.members.length !== 2 || orphanGroup.retention !== "active") {
  throw new Error("Expected an orphan trajectory to remain one current mandatory turn");
}

const assistantTailGroups = groupMessagesByTurn([
  createAnonymousMessage("user", "user starts the turn"),
  createAnonymousMessage("assistant", "assistant remains in the user turn")
]);

if (assistantTailGroups.length !== 1 || assistantTailGroups[0]?.members.length !== 2) {
  throw new Error("Expected a trailing assistant message to remain in its preceding user turn");
}

const dataOnlyGroups = groupMessagesByTurn([
  { id: "data-workspace-metadata-message", role: "assistant", data: { toolName: "write_file" } },
  createAnonymousMessage("user", "current question after custom data")
]);
if (dataOnlyGroups.length !== 1 || dataOnlyGroups[0]?.members[0]?.message.role !== "user") {
  throw new Error("Expected data-only Mastra telemetry messages to stay out of conversation turn grouping");
}

assertThrows(
  () => groupMessagesByTurn([
    createMessage("duplicate-id", "user", "one"),
    createMessage("duplicate-id", "user", "two")
  ]),
  "CONTEXT_DUPLICATE_MESSAGE_ID"
);

const conversationSource = new MastraConversationContextAdapter({
  messages: [
    createMessage("source-user", "user", "source question"),
    createMessage("source-assistant", "assistant", "source answer"),
    createMessage("memory-summary:source-summary", "user", "<conversation_summary>compact</conversation_summary>")
  ],
  systemMessages: [{ role: "system", content: "source system" }]
});
const conversationSourceItems = conversationSource.collect();
if (!conversationSourceItems.some((item) => item.metadata.messageKind === "system")) {
  throw new Error("Expected conversation source to emit system inventory items");
}
if (!conversationSourceItems.some((item) =>
  item.sourceType === "compact-conversation-memory"
  && item.metadata.sourceOwner === "metadata-summary"
  && item.metadata.messageKind === "source-message"
)) {
  throw new Error("Expected conversation source to classify memory-summary as compact memory source");
}
const conversationPackage = builder.build(conversationSourceItems, identityToPackageOptions(identity));
const conversationGroupPlan = new ContextPromptMaterializer().createGroupPlan({
  contextPackage: conversationPackage
});
if (
  conversationGroupPlan.systemMessages.length !== 1
  || conversationGroupPlan.systemMessages[0]?.content !== "source system"
) {
  throw new Error("Expected system messages to be materialized from ContextPackage inventory");
}

const alignmentState = new ContextRunState({ ...identity, runId: "context-alignment-run" });
const alignmentProcessor = new MastraContextBudgetProcessor({
  eventSink: createMemoryEventSink(),
  modelName: "context-smoke-model",
  runState: alignmentState
});
alignmentProcessor.processInputStep({
  messages: anonymousMessages,
  systemMessages: [],
  tools: {},
  stepNumber: 0
});
const inventoryTurnIds = new Set(
  alignmentState.package.groups.filter((group) => group.kind === "turn").map((group) => group.id)
);
const plannedTurnIds = new Set(alignmentState.plans[0]?.selectedGroupIds ?? []);

if (inventoryTurnIds.size !== plannedTurnIds.size || [...inventoryTurnIds].some((id) => !plannedTurnIds.has(id))) {
  throw new Error("Expected processor inventory group IDs to align with planner group IDs");
}

const replacementState = new ContextRunState({ ...identity, runId: "context-replacement-run" });
const replacementProcessor = new MastraContextBudgetProcessor({
  eventSink: createMemoryEventSink(),
  modelName: "context-smoke-model",
  runState: replacementState
});
replacementProcessor.processInputStep({
  messages: [
    createMessage("replace-user-1", "user", "first question"),
    createMessage("replace-assistant-1", "assistant", "first answer"),
    createMessage("replace-user-2", "user", "second question"),
    createMessage("replace-assistant-old", "assistant", "old current answer")
  ],
  systemMessages: [],
  tools: {},
  stepNumber: 0
});
const replacementResult = replacementProcessor.processInputStep({
  messages: [
    createMessage("replace-user-1", "user", "first question"),
    createMessage("replace-assistant-1", "assistant", "first answer"),
    createMessage("replace-user-2", "user", "second question"),
    createMessage("replace-assistant-new", "assistant", "new current answer")
  ],
  systemMessages: [],
  tools: {},
  stepNumber: 1
});
const replacementIds = replacementResult.messages.map((message) => message.id).join(",");
if (replacementIds !== "replace-user-1,replace-assistant-1,replace-user-2,replace-assistant-new") {
  throw new Error(`Expected protocol message snapshot replacement, got: ${replacementIds}`);
}
if (replacementState.package.items.some((item) => item.id === "message-replace-assistant-old")) {
  throw new Error("Expected stale assistant message inventory item to be removed on the next input step");
}

const agUiStepState = new ContextRunState({ ...identity, runId: "context-ag-ui-step-run" });
const agUiStepProcessor = new MastraContextBudgetProcessor({
  eventSink: createMemoryEventSink(),
  modelName: "context-smoke-model",
  runState: agUiStepState
});
const agUiStepMessageList = new MessageList();
agUiStepMessageList.add(convertAGUIMessagesToMastra([
  { id: "hello-user", role: "user", content: "你好" },
  { id: "hello-assistant", role: "assistant", content: "你好 blabla" },
  { id: "analysis-user", role: "user", content: "分析很多数据" }
]), "user");
agUiStepProcessor.processInputStep({
  messages: agUiStepMessageList.get.all.db(),
  messageList: agUiStepMessageList,
  systemMessages: [],
  tools: {},
  stepNumber: 0
});
agUiStepMessageList.add(convertAGUIMessagesToMastra([
  {
    role: "assistant",
    content: "",
    toolCalls: [{ id: "tc-ag-ui", type: "function", function: { name: "inspect_schema", arguments: "{}" } }]
  },
  { role: "tool", content: "{\"tables\":[]}", toolCallId: "tc-ag-ui" }
]), "response");
agUiStepProcessor.processInputStep({
  messages: agUiStepMessageList.get.all.db(),
  messageList: agUiStepMessageList,
  systemMessages: [],
  tools: {},
  stepNumber: 1
});
const agUiStepMessages = agUiStepMessageList.get.all.db();
const helloAssistantIndex = agUiStepMessages.findIndex((message) => promptText(message) === "你好 blabla");
const analysisUserIndex = agUiStepMessages.findIndex((message) => promptText(message) === "分析很多数据");
const toolStepIndex = agUiStepMessages.findIndex((message) => messageContainsToolInvocation(message, "tc-ag-ui"));
if (
  helloAssistantIndex !== 1
  || analysisUserIndex <= helloAssistantIndex
  || toolStepIndex <= analysisUserIndex
) {
  throw new Error(`Expected AG-UI history assistant to keep chronological order, got: ${
    agUiStepMessages.map(messageOrderLabel).join(" | ")
  }`);
}

const profileRegistry = new ModelContextProfileRegistry({
  defaultProfile: {
    id: "context-smoke-small",
    modelPattern: "*",
    contextWindow: 420,
    outputReserve: 40,
    safetyMargin: 20,
    messageOverhead: 4,
    toolSchemaOverhead: 8
  }
});
const promptMaterializer = new ContextPromptMaterializer();
const sourcePackage = builder.build([
  createContextItem({
    id: "source-memory-model",
    sourceType: "long-term-memory",
    sourceId: "memory-1",
    groupId: "long-term-memory",
    visibility: "model",
    trust: "memory",
    retention: "supporting",
    priority: 35,
    content: "<long_term_memory>\n- remember refund rate\n</long_term_memory>",
    metadata: createContextSourceMetadata({
      dedupeKeys: ["long-term-memory:memory-1"],
      exclusivityKey: "long-term-memory:metadata-ltm",
      sourceKind: "long-term-memory",
      sourceOwner: "metadata-ltm"
    }, { groupKind: "source", atomic: false })
  })
], identityToPackageOptions(identity));
const sourceGroups = promptMaterializer.createGroups({
  contextPackage: sourcePackage
});
const sourcePromptGroup = sourceGroups.find((group) => group.id === "long-term-memory");
if (!sourcePromptGroup?.messages.some((message) => message.id === "context:long-term-memory")) {
  throw new Error("Expected ContextPromptMaterializer to materialize runtime source inventory");
}
const sourcePromptView = promptMaterializer.materializePromptView({
  groups: sourceGroups,
  selectedGroupIds: new Set(["long-term-memory"]),
  systemMessages: [],
  tokenReport: {
    countQuality: "estimated",
    inputBudget: 1000,
    messageTokens: 1,
    remainingTokens: 999,
    systemTokens: 0,
    toolTokens: 0,
    totalInputTokens: 1
  }
});
if (sourcePromptView.messages.length !== 1 || sourcePromptView.messages[0]?.id !== "context:long-term-memory") {
  throw new Error("Expected ContextPromptMaterializer to build ContextPromptView from selected groups");
}
const customSourceMaterializer = new ContextPromptMaterializer({
  sourceMaterializer: {
    id: "smoke-source-materializer",
    materialize: (input) => ({
      id: `custom-context:${input.groupId}`,
      role: "system",
      content: `custom source items=${input.items.length}`
    })
  }
});
const customSourceGroups = customSourceMaterializer.createGroups({
  contextPackage: sourcePackage
});
const customSourceMessage = customSourceGroups.find((group) => group.id === "long-term-memory")?.messages[0];
if (
  customSourceMessage?.id !== "custom-context:long-term-memory"
  || customSourceMessage.role !== "system"
  || customSourceMessage.content !== "custom source items=1"
) {
  throw new Error("Expected source prompt materialization policy to be replaceable");
}

const boundarySourceState = new ContextRunState({ ...identity, runId: "context-boundary-source-materializer-run" });
const boundaryToolObservation = createToolObservationBoundary({ identity: boundarySourceState.identity });
const boundaryDispatcher = new ToolObservationDispatcher(boundaryToolObservation.packager, {
  modelName: "context-smoke-model",
  resourceId: identity.resourceId,
  runId: boundarySourceState.identity.runId,
  sessionId: identity.sessionId
});
const boundaryProcessorBundle = createMastraContextProcessorBoundary({
  additionalRuntimeSources: [{
    sourceType: "boundary-runtime-source",
    collect: () => [
      createContextItem({
        id: "boundary-runtime-source-model",
        sourceType: "boundary-runtime-source",
        sourceId: "boundary-runtime-source",
        groupId: "boundary-runtime-source",
        visibility: "model",
        trust: "runtime",
        retention: "supporting",
        priority: 20,
        content: "boundary runtime source",
        metadata: createContextSourceMetadata({
          dedupeKeys: ["boundary-runtime-source:model"],
          exclusivityKey: "boundary-runtime-source",
          sourceKind: "boundary-runtime-source",
          sourceOwner: "smoke"
        }, { atomic: false, groupKind: "source" })
      })
    ]
  }],
  contextCompilation: {
    sourceMaterializer: {
      id: "boundary-source-materializer",
      materialize: (input) => ({
        id: `boundary-context:${input.groupId}`,
        role: "system",
        content: `boundary source items=${input.items.length}`
      })
    }
  },
  dispatcher: boundaryDispatcher,
  eventSink: createMemoryEventSink(),
  modelName: "context-smoke-model",
  runScope: {
    runId: boundarySourceState.identity.runId,
    sessionId: identity.sessionId,
    userId: identity.resourceId
  },
  runState: boundarySourceState
});
const boundaryProcessors = boundaryProcessorBundle.inputProcessors;
const boundaryOutputProcessors = boundaryProcessorBundle.outputProcessors;
const boundaryRuntimeSourceProcessor = boundaryProcessors.find((processor) => processor.id === "context-runtime-source");
const boundaryBudgetProcessor = boundaryProcessors.find((processor) => processor.id === "context-budget");
const customDataFilterProcessor = boundaryOutputProcessors.find((processor) => processor.id === "custom-data-part-filter");
if (!customDataFilterProcessor?.processOutputStream) {
  throw new Error("Expected Mastra protocol boundary to include the custom data part filter output processor");
}
const customDataFilterResult = await customDataFilterProcessor.processOutputStream({
  part: {
    type: "data-workspace-metadata",
    data: { toolName: "write_file" }
  },
  streamParts: [],
  state: {}
});
if (customDataFilterResult !== null) {
  throw new Error("Expected Mastra custom data parts to be filtered at the protocol boundary");
}
const normalStreamPart = {
  type: "text-delta",
  payload: { text: "model-visible text" }
};
const normalFilterResult = await customDataFilterProcessor.processOutputStream({
  part: normalStreamPart,
  streamParts: [],
  state: {}
});
if (normalFilterResult !== normalStreamPart) {
  throw new Error("Expected non-data stream parts to pass through the protocol boundary filter");
}
await boundaryRuntimeSourceProcessor?.processInputStep({
  messages: [],
  systemMessages: [],
  tools: {},
  stepNumber: 0
});
const boundaryResult = boundaryBudgetProcessor?.processInputStep({
  messages: [createMessage("boundary-current-user", "user", "continue")],
  systemMessages: [],
  tools: {},
  stepNumber: 1
});
if (!boundaryResult?.messages.some((message) => message.id === "boundary-context:boundary-runtime-source")) {
  throw new Error("Expected Mastra context processor boundary to inject source materializer");
}
const turnInventoryPackage = builder.build([
  createConversationMessageItem("turn-user", "inventory-turn", createMessage("turn-user", "user", "from inventory")),
  createConversationMessageItem(
    "turn-assistant",
    "inventory-turn",
    createMessage("turn-assistant", "assistant", "also from inventory")
  )
], identityToPackageOptions(identity));
const inventoryTurnGroups = promptMaterializer.createGroups({
  contextPackage: turnInventoryPackage
});
const inventoryTurnGroup = inventoryTurnGroups.find((group) => group.id === "inventory-turn");
if (
  !inventoryTurnGroup
  || inventoryTurnGroup.messages.length !== 2
  || inventoryTurnGroup.messages[0]?.id !== "turn-user"
  || inventoryTurnGroup.messages[1]?.id !== "turn-assistant"
) {
  throw new Error("Expected ContextPromptMaterializer to build turn groups from ContextPackage inventory");
}
const sourceItem = sourcePackage.items[0];
if (
  !sourceItem
  || contextItemSourceKind(sourceItem) !== "long-term-memory"
  || contextItemSourceOwner(sourceItem) !== "metadata-ltm"
  || contextItemExclusivityKey(sourceItem) !== "long-term-memory:metadata-ltm"
) {
  throw new Error("Expected source metadata helpers to read long-term memory source metadata");
}
const sourceSnapshot = sourcePackage.sourceSnapshots.find((snapshot) => snapshot.sourceType === "long-term-memory");
if (
  !sourceSnapshot
  || !sourceSnapshot.metadata.sourceKinds.includes("long-term-memory")
  || !sourceSnapshot.metadata.sourceOwners.includes("metadata-ltm")
  || !sourceSnapshot.metadata.exclusivityKeys.includes("long-term-memory:metadata-ltm")
) {
  throw new Error("Expected ContextPackage source snapshot to retain source metadata summary");
}
const overlapContent = "Refund rate increased for orders in Q2.";
const ltmSource = new LongTermMemoryContextSource({
  records: [{
    id: "ltm-overlap-1",
    scope: "user",
    kind: "preference",
    content_text: overlapContent,
    confidence: 0.9
  }]
});
const knowledgeAdapter = new RetrieveKnowledgeToolObservationAdapter();
const overlapPackage = builder.build([
  ...ltmSource.collect({
    budget: { maxChars: 4096 },
    runId: identity.runId,
    sessionId: identity.sessionId,
    userId: identity.resourceId
  }),
  ...knowledgeAdapter.toContextItems({
    collection_id: "knowledge-overlap",
    chunks: [{
      chunk_id: "chunk-overlap-1",
      document_id: "doc-overlap-1",
      filename: "orders.md",
      quote: overlapContent,
      content: overlapContent,
      score: 1
    }]
  }, { maxChars: 4096 })
], identityToPackageOptions(identity));
const overlapPolicyResult = sourcePolicy.applyPackage(overlapPackage);
const overlapPlan = promptMaterializer.createGroupPlan({
  contextPackage: overlapPackage,
  sourceItemIds: new Set(overlapPolicyResult.items.map((item) => item.id))
});
if (!overlapPolicyResult.decisions.some((decision) => decision.reason === "cross_source_overlap_flagged")) {
  throw new Error("Expected memory/knowledge content overlap to be reported by source policy");
}
if (!overlapPlan.groups.some((group) => group.id === "long-term-memory")) {
  throw new Error("Expected overlapping long-term memory to remain model-visible");
}
if (!overlapPlan.groups.some((group) => group.id === "knowledge-retrieval-observation")) {
  throw new Error("Expected overlapping knowledge evidence to remain model-visible");
}
const knowledgeSnapshot = overlapPackage.sourceSnapshots.find((snapshot) =>
  snapshot.sourceType === "knowledge-retrieval"
);
if (
  !knowledgeSnapshot
  || !knowledgeSnapshot.metadata.sourceKinds.includes("knowledge")
  || !knowledgeSnapshot.metadata.sourceOwners.includes("knowledge-retrieval")
  || knowledgeSnapshot.metadata.overlapKeys.length === 0
) {
  throw new Error("Expected knowledge source snapshot to retain overlap metadata");
}
const overlapProcessorState = new ContextRunState({ ...identity, runId: "context-overlap-audit-run" });
overlapProcessorState.merge(overlapPackage);
const overlapEvents = [];
const overlapProcessor = new MastraContextBudgetProcessor({
  eventSink: createMemoryEventSink(overlapEvents),
  modelName: "context-smoke-model",
  planner: new ContextStepPlanner(),
  runState: overlapProcessorState
});
overlapProcessor.processInputStep({
  messages: [createMessage("overlap-current-user", "user", "compare memory and knowledge")],
  systemMessages: [],
  tools: {},
  stepNumber: 1
});
const overlapCompiledEvent = overlapEvents.find((event) => event.name === "context.compiled");
if (!overlapCompiledEvent?.value?.decisions?.some((decision) =>
  decision.reason === "cross_source_overlap_flagged"
)) {
  throw new Error("Expected context.compiled decisions to include overlap flags");
}
if (overlapCompiledEvent.value.omitted_sources.length !== 0) {
  throw new Error("Expected overlap-only source policy decisions to stay out of omitted_sources");
}
const duplicateCompactPackage = builder.build([
  createCompactMemoryItem("compact-metadata", "metadata-summary", 40, "authoritative summary"),
  createCompactMemoryItem("compact-working", "mastra-working-memory", 45, "projection summary"),
  createContextItem({
    id: "compact-shadow",
    sourceType: "compact-conversation-memory",
    sourceId: "shadow-summary",
    groupId: "compact-conversation-memory",
    visibility: "model",
    trust: "memory",
    retention: "supporting",
    priority: 50,
    content: "shadow summary",
    metadata: createContextSourceMetadata({
      dedupeKeys: ["compact-conversation-memory:shadow"],
      exclusivityKey: "compact-conversation-memory",
      shadow: true,
      sourceKind: "compact-conversation-memory",
      sourceOwner: "observational-summary"
    }, { groupKind: "source", atomic: false })
  })
], identityToPackageOptions(identity));
const duplicateCompactPolicyResult = sourcePolicy.applyPackage(duplicateCompactPackage);
const duplicateCompactPlan = promptMaterializer.createGroupPlan({
  contextPackage: duplicateCompactPackage,
  sourceItemIds: new Set(duplicateCompactPolicyResult.items.map((item) => item.id))
});
const compactGroup = duplicateCompactPlan.groups.find((group) => group.id === "compact-conversation-memory");
const compactText = promptText(compactGroup?.messages[0]);
if (!compactText.includes("authoritative summary")) {
  throw new Error("Expected compact memory policy to keep metadata-summary authority");
}
if (compactText.includes("projection summary")) {
  throw new Error("Expected compact memory policy to omit non-authoritative projection");
}
if (
  !duplicateCompactPolicyResult.decisions.some((decision) =>
    decision.reason === "non_authoritative_projection_omitted"
  )
) {
  throw new Error("Expected compact memory policy to record non-authoritative omission");
}
if (
  !duplicateCompactPolicyResult.decisions.some((decision) => decision.reason === "shadow_source_not_model_visible")
) {
  throw new Error("Expected compact memory policy to record shadow omission");
}
const events = [];
const processor = new MastraContextBudgetProcessor({
  eventSink: createMemoryEventSink(events),
  modelName: "context-smoke-model",
  planner: new ContextStepPlanner({ profileRegistry }),
  runState
});
const messages = [
  createMessage("old-user", "user", "old question ".repeat(120)),
  createMessage("old-assistant", "assistant", "old response ".repeat(80)),
  createMessage("current-user", "user", "current question"),
  createMessage("current-assistant", "assistant", "current response"),
  createToolResultMessage("current-tool-result")
];
const processorResult = processor.processInputStep({
  messages,
  systemMessages: [{ role: "system", content: "read-only data agent" }],
  tools: {},
  stepNumber: 1
});

if (!processorResult?.messages || processorResult.messages.some((message) => message.id === "old-user")) {
  throw new Error("Expected the default reduction strategy to omit the complete oldest turn");
}
if (!processorResult.messages.some((message) => message.id === "current-user")) {
  throw new Error("Expected the mandatory current turn to remain selected");
}
if (!processorResult.messages.some((message) => message.id === "current-tool-result")) {
  throw new Error("Expected the active tool result to remain atomic with its current turn");
}
if (
  processorResult.systemMessages?.length !== 1
  || processorResult.systemMessages[0]?.content !== "read-only data agent"
) {
  throw new Error("Expected processor system messages to be projected from ContextPackage inventory");
}
if (!events.some((event) => event.name === "context.compiled")) {
  throw new Error("Expected context compilation to emit a bounded AG-UI CUSTOM audit event");
}
// R-017: context.compiled must carry stable top-level budget fields.
const compiledEvent = events.find((event) => event.name === "context.compiled");
if (!compiledEvent) {
  throw new Error("Expected a context.compiled event to inspect for R-017 budget fields");
}
const compiledValue = compiledEvent.value ?? {};
if (typeof compiledValue.total_tokens !== "number") {
  throw new Error("context.compiled must expose top-level total_tokens (R-017)");
}
if (typeof compiledValue.budget_tokens !== "number") {
  throw new Error("context.compiled must expose top-level budget_tokens (R-017)");
}
if (typeof compiledValue.prompt_tokens !== "number") {
  throw new Error("context.compiled must expose top-level prompt_tokens (R-017)");
}
if (typeof compiledValue.remaining_tokens !== "number") {
  throw new Error("context.compiled must expose top-level remaining_tokens (R-017)");
}

const compactProcessorState = new ContextRunState({ ...identity, runId: "context-compact-memory-run" });
compactProcessorState.merge(builder.build([
  createContextItem({
    id: "compact-working-shadow",
    sourceType: "compact-conversation-memory",
    sourceId: "mastra-working-memory-shadow",
    groupId: "compact-conversation-memory",
    visibility: "model",
    trust: "memory",
    retention: "supporting",
    priority: 45,
    content: "shadow working memory projection",
    metadata: createContextSourceMetadata({
      dedupeKeys: ["compact-conversation-memory:mastra-working-memory-shadow"],
      exclusivityKey: "compact-conversation-memory",
      shadow: true,
      sourceKind: "compact-conversation-memory",
      sourceOwner: "mastra-working-memory"
    }, { atomic: false, groupKind: "source" })
  })
], identityToPackageOptions(compactProcessorState.identity)));
const compactEvents = [];
const compactProcessor = new MastraContextBudgetProcessor({
  eventSink: createMemoryEventSink(compactEvents),
  modelName: "context-smoke-model",
  planner: new ContextStepPlanner({ profileRegistry }),
  runState: compactProcessorState
});
const compactProcessorResult = compactProcessor.processInputStep({
  messages: [
    createMessage("memory-summary:summary-1", "user", "<conversation_summary>prior summary</conversation_summary>"),
    createMessage("compact-current-user", "user", "continue analysis")
  ],
  systemMessages: [],
  tools: {},
  stepNumber: 2
});
if (!compactProcessorResult.messages.some((message) => message.id === "context:compact-conversation-memory")) {
  throw new Error("Expected memory-summary message to be compiled as compact memory source");
}
if (compactProcessorResult.messages.some((message) => message.id === "memory-summary:summary-1")) {
  throw new Error("Expected raw memory-summary message to be omitted after source materialization");
}
if (!compactProcessorResult.messages.some((message) => message.id === "compact-current-user")) {
  throw new Error("Expected current user message to remain after compact memory source materialization");
}
const compactSnapshot = compactProcessorState.package.sourceSnapshots.find((snapshot) =>
  snapshot.sourceType === "compact-conversation-memory"
);
if (
  !compactSnapshot
  || !compactSnapshot.metadata.sourceKinds.includes("compact-conversation-memory")
  || !compactSnapshot.metadata.sourceOwners.includes("metadata-summary")
) {
  throw new Error("Expected compact memory source snapshot metadata");
}
const compactCompiledEvent = compactEvents.find((event) => event.name === "context.compiled");
if (
  !compactCompiledEvent?.value?.selected_sources?.some((source) =>
    source.group_id === "compact-conversation-memory"
    && source.source_kinds.includes("compact-conversation-memory")
    && source.source_owners.includes("metadata-summary")
  )
) {
  throw new Error("Expected context.compiled to include compact memory selected source metadata");
}
const compactSelectedSource = compactCompiledEvent?.value?.selected_sources?.find((source) =>
  source.group_id === "compact-conversation-memory"
);
if (compactSelectedSource?.source_owners.includes("mastra-working-memory")) {
  throw new Error("Expected selected source metadata to exclude omitted shadow WorkingMemory owner");
}
if (
  !compactCompiledEvent?.value?.omitted_sources?.some((source) =>
    source.group_id === "compact-conversation-memory"
    && source.source_owners.includes("mastra-working-memory")
    && source.shadow === true
  )
) {
  throw new Error("Expected omitted source metadata to include shadow WorkingMemory owner");
}
if (
  !compactCompiledEvent?.value?.decisions?.some((decision) =>
    decision.reason === "shadow_source_not_model_visible"
    && decision.affectedItemIds?.includes("compact-working-shadow")
  )
) {
  throw new Error("Expected context.compiled decisions to include source-level affected item IDs");
}

const legacyLtmProcessorState = new ContextRunState({ ...identity, runId: "context-legacy-ltm-message-run" });
const legacyLtmProcessor = new MastraContextBudgetProcessor({
  eventSink: createMemoryEventSink(),
  modelName: "context-smoke-model",
  planner: new ContextStepPlanner(),
  runState: legacyLtmProcessorState
});
const legacyLtmProcessorResult = legacyLtmProcessor.processInputStep({
  messages: [
    createMessage("context:long-term-memory", "user", "<long_term_memory>\n- legacy source path\n</long_term_memory>"),
    createMessage("legacy-ltm-current-user", "user", "use memory if relevant")
  ],
  systemMessages: [],
  tools: {},
  stepNumber: 2
});
if (!legacyLtmProcessorResult.messages.some((message) => message.id === "context:long-term-memory")) {
  throw new Error("Expected protocol-carried long-term memory to materialize through the source compiler");
}
if (
  legacyLtmProcessorState.package.items.some((item) =>
    item.sourceType === "long-term-memory"
    && item.metadata.messageKind !== "source-message"
  )
) {
  throw new Error("Expected protocol-carried long-term memory to be classified as a source message");
}
if (!legacyLtmProcessorResult.messages.some((message) => message.id === "legacy-ltm-current-user")) {
  throw new Error("Expected current user message to remain with protocol-carried long-term memory");
}

const workingMemoryState = new ContextRunState({ ...identity, runId: "context-working-memory-run" });
const workingMemoryRegistry = new RuntimeContextSourceRegistry();
workingMemoryRegistry.register(new WorkingMemoryProjectionContextSource({
  memory: {
    getWorkingMemory: async () => "# Conversation Summary\nfrom_position: 1\nto_position: 2\n\nMirrored summary"
  }
}));
const workingMemorySourceProcessor = new MastraContextRuntimeSourceProcessor({
  registry: workingMemoryRegistry,
  runScope: {
    runId: "context-working-memory-run",
    sessionId: identity.sessionId,
    userId: identity.resourceId
  },
  runState: workingMemoryState
});
await workingMemorySourceProcessor.processInputStep({
  messages: [createMessage("working-current-user", "user", "continue")],
  systemMessages: [],
  tools: {},
  stepNumber: 0
});
const workingMemoryEvents = [];
const workingMemoryBudgetProcessor = new MastraContextBudgetProcessor({
  eventSink: createMemoryEventSink(workingMemoryEvents),
  modelName: "context-smoke-model",
  planner: new ContextStepPlanner({ profileRegistry }),
  runState: workingMemoryState
});
const workingMemoryResult = workingMemoryBudgetProcessor.processInputStep({
  messages: [createMessage("working-current-user", "user", "continue")],
  systemMessages: [],
  tools: {},
  stepNumber: 1
});
const workingSnapshot = workingMemoryState.package.sourceSnapshots.find((snapshot) =>
  snapshot.sourceType === "compact-conversation-memory"
);
if (
  !workingSnapshot
  || !workingSnapshot.metadata.sourceOwners.includes("mastra-working-memory")
  || !workingSnapshot.metadata.shadow
) {
  throw new Error("Expected WorkingMemory projection to be recorded as shadow compact memory source");
}
if (workingMemoryResult.messages.some((message) => message.id === "context:compact-conversation-memory")) {
  throw new Error("Expected shadow WorkingMemory projection to stay out of model-visible prompt");
}
const workingCompiled = workingMemoryEvents.find((event) => event.name === "context.compiled");
if (!workingCompiled?.value?.decisions?.some((decision) => decision.reason === "shadow_source_not_model_visible")) {
  throw new Error("Expected context.compiled to record WorkingMemory shadow source omission");
}

let customRuntimeSourceEnabled = true;
const additionalSourceRegistry = createDefaultRuntimeContextSourceRegistry({
  additionalSources: [{
    sourceType: "custom-runtime-source",
    collect: () => customRuntimeSourceEnabled
      ? [
          createContextItem({
            id: "custom-runtime-source-model",
            sourceType: "custom-runtime-source",
            sourceId: "custom-runtime-source",
            groupId: "custom-runtime-source",
            visibility: "model",
            trust: "runtime",
            retention: "supporting",
            priority: 20,
            content: "custom runtime source",
            metadata: createContextSourceMetadata({
              dedupeKeys: ["custom-runtime-source:model"],
              exclusivityKey: "custom-runtime-source",
              sourceKind: "custom-runtime-source",
              sourceOwner: "smoke"
            }, { atomic: false, groupKind: "source" })
          })
        ]
      : []
  }]
});
const additionalSourceState = new ContextRunState({ ...identity, runId: "context-additional-runtime-source-run" });
const additionalSourceProcessor = new MastraContextRuntimeSourceProcessor({
  registry: additionalSourceRegistry,
  runScope: {
    runId: additionalSourceState.identity.runId,
    sessionId: identity.sessionId,
    userId: identity.resourceId
  },
  runState: additionalSourceState
});
await additionalSourceProcessor.processInputStep({
  messages: [createMessage("additional-source-user", "user", "continue")],
  systemMessages: [],
  tools: {},
  stepNumber: 0
});
if (!additionalSourceState.package.sourceSnapshots.some((snapshot) => snapshot.sourceType === "custom-runtime-source")) {
  throw new Error("Expected additional runtime sources to be registered by the source boundary");
}
customRuntimeSourceEnabled = false;
await additionalSourceProcessor.processInputStep({
  messages: [createMessage("additional-source-user-2", "user", "continue again")],
  systemMessages: [],
  tools: {},
  stepNumber: 1
});
if (additionalSourceState.package.sourceSnapshots.some((snapshot) => snapshot.sourceType === "custom-runtime-source")) {
  throw new Error("Expected stale runtime source snapshots to be removed when a source returns no items");
}
const staleSourceBudgetProcessor = new MastraContextBudgetProcessor({
  eventSink: createMemoryEventSink(),
  modelName: "context-smoke-model",
  runState: additionalSourceState
});
const staleSourceResult = staleSourceBudgetProcessor.processInputStep({
  messages: [createMessage("additional-source-user-2", "user", "continue again")],
  systemMessages: [],
  tools: {},
  stepNumber: 2
});
if (staleSourceResult.messages.some((message) => message.id === "context:custom-runtime-source")) {
  throw new Error("Expected stale runtime source content to stay out of the next prompt");
}

const sqlAdapter = new SqlResultToolObservationAdapter();
const invalidSqlItems = sqlAdapter.toContextItems({ error: "tool failed before returning rows" }, { maxChars: 4096 });
const invalidSqlModel = invalidSqlItems.find((item) => item.visibility === "model")?.content;

if (!invalidSqlModel?.tool_result_invalid) {
  throw new Error("Expected malformed SQL tool observations to become bounded invalid-result context");
}

const alreadyGovernedSqlItems = sqlAdapter.toContextItems({
  columns: ["id"],
  rows: [[1]],
  row_count: 1,
  audit_log_id: "audit-1",
  elapsed_ms: 1
}, { maxChars: 4096 });
const alreadyGovernedSqlModel = alreadyGovernedSqlItems.find((item) => item.visibility === "model")?.content;

if (!Array.isArray(alreadyGovernedSqlModel?.rows) || alreadyGovernedSqlModel.rows.length !== 1) {
  throw new Error("Expected already-governed SQL observations to be idempotently accepted");
}
const sqlMetadataItem = alreadyGovernedSqlItems.find((item) => item.visibility === "model");
if (
  !sqlMetadataItem
  || contextItemSourceKind(sqlMetadataItem) !== "tool-observation"
  || contextItemExclusivityKey(sqlMetadataItem) !== "tool-observation:sql"
  || !contextItemDedupeKeys(sqlMetadataItem).includes("tool-observation:sql")
) {
  throw new Error("Expected SQL tool observation to expose source metadata contract");
}

const mcpToolName = "mcp__smoke__big_result";
const mcpObservationBoundary = createToolObservationBoundary({
  identity: { ...identity, runId: "context-mcp-observation-run" },
  mcpToolNames: [mcpToolName]
});
const mcpPackage = mcpObservationBoundary.packager.packageToolObservation({
  toolName: mcpToolName,
  rawResult: "mcp-result ".repeat(2000),
  runScope: {
    modelName: "context-smoke-model",
    resourceId: identity.resourceId,
    runId: "context-mcp-observation-run",
    sessionId: identity.sessionId
  }
});
const mcpModel = mcpPackage.items.find((item) => item.visibility === "model")?.content;
if (!mcpPackage.truncation.some((entry) => entry.sourceId === mcpToolName && entry.truncated)) {
  throw new Error("Expected oversized MCP tool observations to produce truncation metadata");
}
if (
  !mcpModel
  || typeof mcpModel !== "object"
  || !String(mcpModel.content ?? "").includes("[truncated")
  || String(mcpModel.content ?? "").length > 13000
) {
  throw new Error("Expected oversized MCP tool observations to be bounded before model visibility");
}

const schemaAdapter = new SchemaToolObservationAdapter();
const invalidSchemaItems = schemaAdapter.toContextItems({ error: "schema failed before returning tables" }, {
  maxChars: 4096
});
const invalidSchemaModel = invalidSchemaItems.find((item) => item.visibility === "model")?.content;

if (!invalidSchemaModel?.tool_result_invalid) {
  throw new Error("Expected malformed schema tool observations to become bounded invalid-result context");
}

const alreadyGovernedSchemaItems = schemaAdapter.toContextItems({
  datasource_id: "api-duckdb-demo",
  schema_id: "schema-1",
  tables: [{ name: "orders", columns: [{ name: "order_id", type: "INTEGER" }] }]
}, { maxChars: 4096 });
const alreadyGovernedSchemaModel = alreadyGovernedSchemaItems.find((item) => item.visibility === "model")?.content;

if (alreadyGovernedSchemaModel?.schema_id !== "schema-1") {
  throw new Error("Expected already-governed schema observations to preserve schema_id");
}

const customRegistry = new ReductionStrategyRegistry();
customRegistry.register({
  id: "custom-history-policy",
  propose: (state) => state.groups
    .filter((group) => !group.mandatory && state.selectedGroupIds.has(group.id))
    .map((group) => ({
      strategyId: "custom-history-policy",
      removeGroupIds: [group.id],
      expectedTokenSavings: group.tokenCost,
      qualityLoss: 0,
      reason: "custom policy selected this group"
    }))
});
const customPlanner = new ContextStepPlanner({
  profileRegistry,
  strategyRegistry: customRegistry,
  registerDefaultStrategies: false
});
const customGroupPlan = promptMaterializer.createGroupPlan({
  contextPackage: runState.package
});
const customPlanningGroups = customPlanner.createPlanningGroups({
  groups: customGroupPlan.groups,
  modelName: "context-smoke-model"
});
const customResult = customPlanner.plan({
  contextPackage: runState.package,
  groups: customPlanningGroups,
  stepNumber: 2,
  systemMessages: [],
  tools: {},
  modelName: "context-smoke-model",
  sourceDecisions: []
});

if (customResult.plan.decisions[0]?.strategyId !== "custom-history-policy") {
  throw new Error("Expected a custom reduction strategy to work without modifying the planner");
}

const guardEvents = [];
const guard = new MastraProviderPromptGuardProcessor({
  eventSink: createMemoryEventSink(guardEvents),
  modelName: "context-smoke-model",
  profileRegistry
});
assertThrows(
  () => guard.processLLMRequest({
    prompt: [{ role: "user", content: [{ type: "text", text: "large prompt ".repeat(300) }] }],
    stepNumber: 3,
    abort: (reason) => {
      throw new Error(reason);
    }
  }),
  "CONTEXT_FINAL_PROMPT_EXCEEDS_BUDGET"
);

if (!guardEvents.some((event) => event.name === "context.prompt-verified")) {
  throw new Error("Expected the provider prompt guard to emit verification metrics before aborting");
}
// R-017: context.prompt-verified must carry stable top-level budget fields.
const verifiedEvent = guardEvents.find((event) => event.name === "context.prompt-verified");
const verifiedValue = verifiedEvent?.value ?? {};
if (typeof verifiedValue.prompt_tokens !== "number") {
  throw new Error("context.prompt-verified must expose top-level prompt_tokens (R-017)");
}
if (typeof verifiedValue.remaining_tokens !== "number") {
  throw new Error("context.prompt-verified must expose top-level remaining_tokens (R-017)");
}
if (typeof verifiedValue.budget_tokens !== "number") {
  throw new Error("context.prompt-verified must expose top-level budget_tokens (R-017)");
}
if (typeof verifiedValue.total_tokens !== "number") {
  throw new Error("context.prompt-verified must expose top-level total_tokens (R-017)");
}

const configuredAgent = await createDataFoundry({
  dataGateway: {},
  emitter: { emit: () => undefined },
  modelProvider: {
    kind: "openai-compatible",
    model_name: "context-smoke/model",
    model: { id: "context-smoke/model", url: "http://127.0.0.1:1", apiKey: "unused" }
  },
  runContext: {
    user_id: identity.resourceId,
    session_id: identity.sessionId,
    run_id: identity.runId,
    user_input: "context smoke",
    chat_mode: "smoke",
    selected_datasource_id: "context-smoke-source",
    model_name: "context-smoke/model"
  },
  messages: [
    { id: "activity-message", role: "activity", activityType: "test", content: {}, replace: true },
    { id: "user-message", role: "user", content: "context smoke" }
  ]
});
const configuredProcessors = await configuredAgent.agent.listConfiguredInputProcessors();
const configuredProcessorIds = configuredProcessors.map((entry) => entry.id);

if (!configuredProcessorIds.includes("context-budget") || !configuredProcessorIds.includes("provider-prompt-guard")) {
  throw new Error(`Expected Mastra context processors, got ${configuredProcessorIds.join(",")}`);
}
if (configuredAgent.governedMessages.some((message) => message.role === "activity")) {
  throw new Error("Expected ingress governance to remove AG-UI activity messages before Mastra conversion");
}

console.log(
  `Context compilation smoke OK: revision=${runState.package.revision}, ` +
    `plans=${runState.plans.length}, processors=${configuredProcessorIds.length}`
);

function createMessage(id, role, text) {
  return { id, role, content: { format: 2, parts: [{ type: "text", text }] }, createdAt: new Date() };
}

function createAnonymousMessage(role, text) {
  return { role, content: { format: 2, parts: [{ type: "text", text }] }, createdAt: new Date() };
}

function createToolCallMessage() {
  return {
    role: "assistant",
    content: {
      format: 2,
      parts: [{
        type: "tool-invocation",
        toolInvocation: { state: "call", toolCallId: "anonymous-tool-call", toolName: "inspect_schema", args: {} }
      }]
    },
    createdAt: new Date()
  };
}

function createAnonymousToolResultMessage() {
  return {
    role: "user",
    content: {
      format: 2,
      parts: [{
        type: "tool-invocation",
        toolInvocation: {
          state: "result",
          toolCallId: "anonymous-tool-call",
          toolName: "inspect_schema",
          result: {}
        }
      }]
    },
    createdAt: new Date()
  };
}

function createToolResultMessage(id) {
  return {
    id,
    role: "user",
    content: {
      format: 2,
      parts: [{
        type: "tool-invocation",
        toolInvocation: { state: "result", toolCallId: "tool-call-1", toolName: "inspect_schema", result: {} }
      }]
    },
    createdAt: new Date()
  };
}

function createCompactMemoryItem(id, sourceOwner, priority, content) {
  return createContextItem({
    id,
    sourceType: "compact-conversation-memory",
    sourceId: id,
    groupId: "compact-conversation-memory",
    visibility: "model",
    trust: "memory",
    retention: "supporting",
    priority,
    content,
    metadata: createContextSourceMetadata({
      dedupeKeys: [`compact-conversation-memory:${id}`],
      exclusivityKey: "compact-conversation-memory",
      sourceKind: "compact-conversation-memory",
      sourceOwner
    }, { groupKind: "source", atomic: false })
  });
}

function createConversationMessageItem(id, groupId, message) {
  return createContextItem({
    id: `message-${id}`,
    sourceType: "conversation",
    sourceId: id,
    groupId,
    visibility: "model",
    trust: "untrusted-client",
    retention: "active",
    priority: 80,
    content: message,
    metadata: createContextSourceMetadata({
      dedupeKeys: [`message:${id}`],
      exclusivityKey: `conversation-turn:${groupId}`,
      sourceKind: "conversation",
      sourceOwner: "conversation-history"
    }, { atomic: true, groupKind: "turn", mandatory: true, messageKind: "message", role: message.role })
  });
}

function identityToPackageOptions(value) {
  return { resourceId: value.resourceId, sessionId: value.sessionId, runId: value.runId };
}

function promptText(message) {
  const parts = message?.content?.parts;
  return Array.isArray(parts)
    ? parts.filter((part) => part.type === "text").map((part) => part.text).join("\n")
    : "";
}

function messageContainsToolInvocation(message, toolCallId) {
  const parts = message?.content?.parts;
  return Array.isArray(parts) && parts.some((part) =>
    part.type === "tool-invocation" && part.toolInvocation?.toolCallId === toolCallId
  );
}

function messageOrderLabel(message) {
  const text = promptText(message);
  const toolPart = message?.content?.parts?.find?.((part) => part.type === "tool-invocation");
  const toolState = toolPart?.toolInvocation?.state;
  return `${message.role}:${text || toolState || message.id}`;
}

function createMemoryEventSink(events = []) {
  return {
    emitContextEvent: (name, value) => events.push({ name, value })
  };
}

function assertThrows(callback, expectedMessage) {
  try {
    callback();
  } catch (error) {
    if (error instanceof Error && error.message.includes(expectedMessage)) {
      return;
    }
    throw error;
  }
  throw new Error(`Expected error containing: ${expectedMessage}`);
}
