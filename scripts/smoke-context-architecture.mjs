import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";

const repoRoot = process.cwd();
const contextRoot = path.join(repoRoot, "packages/agent-runtime/src/context");

const requiredConceptualLayerDirs = [
  "inventory",
  "policy",
  "projection",
  "protocol",
  "protocol/ag-ui",
  "protocol/mastra",
  "source"
];

const requiredSourceSubdomainDirs = [
  "tool-observation",
  "tool-observation/adapters"
];

const requiredFiles = [
  "inventory/context-item.ts",
  "inventory/context-budget.ts",
  "inventory/context-limits.ts",
  "inventory/context-package-builder.ts",
  "inventory/context-package.ts",
  "inventory/context-plan.ts",
  "inventory/context-run-state.ts",
  "inventory/context-source-metadata.ts",
  "inventory/context-text.ts",
  "inventory/context-token-report.ts",
  "policy/context-budget-allocator.ts",
  "policy/context-policy.ts",
  "policy/context-reduction-strategy.ts",
  "policy/context-source-authority-profile.ts",
  "policy/context-source-policy.ts",
  "policy/context-step-planner.ts",
  "policy/context-token-counter.ts",
  "policy/model-context-profile.ts",
  "policy/prompt-token-counter.ts",
  "projection/context-prompt-materializer.ts",
  "projection/context-prompt-message.ts",
  "projection/context-prompt-view.ts",
  "projection/context-source-prompt-materializer.ts",
  "protocol/ag-ui/ag-ui-context-event-sink.ts",
  "protocol/context-protocol-adapter.ts",
  "protocol/context-protocol-event-sink.ts",
  "protocol/mastra/mastra-context-budget-processor.ts",
  "protocol/mastra/mastra-context-compiled-event.ts",
  "protocol/mastra/mastra-context-processor-boundary.ts",
  "protocol/mastra/mastra-context-protocol-adapter.ts",
  "protocol/mastra/mastra-context-runtime-source-processor.ts",
  "protocol/mastra/mastra-custom-data-part-filter-processor.ts",
  "protocol/mastra/mastra-context-prompt-message-adapter.ts",
  "protocol/mastra/mastra-context-source-message.ts",
  "protocol/mastra/mastra-conversation-context-adapter.ts",
  "protocol/mastra/mastra-message-utils.ts",
  "protocol/mastra/mastra-tool-observation-router.ts",
  "protocol/mastra/mastra-provider-prompt-guard-processor.ts",
  "protocol/mastra/mastra-task-state-context-processor.ts",
  "source/long-term-memory-context-source.ts",
  "source/runtime-context-source-boundary.ts",
  "source/runtime-context-source-registry.ts",
  "source/runtime-context-source.ts",
  "source/working-memory-projection-context-source.ts",
  "tool-observation/adapters/base-tool-observation-adapter.ts",
  "tool-observation/adapters/collaboration-tool-observation-adapters.ts",
  "tool-observation/adapters/data-tool-observation-adapters.ts",
  "tool-observation/adapters/mcp-tool-observation-adapter.ts",
  "tool-observation/adapters/schema-tool-observation-adapter.ts",
  "tool-observation/adapters/sql-result-tool-observation-adapter.ts",
  "tool-observation/adapters/task-tool-observation-adapters.ts",
  "tool-observation/adapters/workspace-tool-observation-adapters.ts",
  "tool-observation/default-tool-observation-adapters.ts",
  "tool-observation/tool-observation-adapter-registry.ts",
  "tool-observation/tool-observation-adapter.ts",
  "tool-observation/tool-observation-budget-profile.ts",
  "tool-observation/tool-observation-boundary.ts",
  "tool-observation/tool-observation-dispatcher.ts",
  "tool-observation/tool-observation-history.ts",
  "tool-observation/tool-observation-packager.ts",
  "tool-observation/tool-observation-projection-items.ts",
  "tool-observation/tool-observation-projection-policy.ts",
  "tool-observation/tool-observation-run-scope.ts"
];

const bannedLegacyPaths = [
  "adapters",
  "context-budget-allocator.ts",
  "context-limits.ts",
  "context-budget-processor.ts",
  "context-orchestrator.ts",
  "context-package-builder.ts",
  "context-package.ts",
  "context-policy.ts",
  "context-reduction-strategy.ts",
  "context-run-state.ts",
  "context-source-registry.ts",
  "context-text-policy.ts",
  "defaults.ts",
  "mastra-message-utils.ts",
  "model-context-profile.ts",
  "policy/context-planner.ts",
  "prompt-token-counter.ts",
  "provider-prompt-guard-processor.ts",
  "schema-context-adapter.ts",
  "sql-result-context-adapter.ts",
  "step-context-planner.ts",
  "task-state-context-processor.ts",
  "token-counter.ts",
  "protocol/mastra/context-budget-processor.ts",
  "protocol/mastra/context-runtime-source-processor.ts",
  "protocol/mastra/mastra-conversation-message-context-source.ts",
  "protocol/mastra/provider-prompt-guard-processor.ts",
  "protocol/mastra/task-state-context-processor.ts",
  "source/mastra-context-runtime-source-processor.ts",
  "source/context-source-metadata.ts",
  "source/conversation-messages-context-source.ts",
  "tool-observation/tool-observation-router.ts",
  "tool-observation-router.ts",
  "tool-result-adapter.ts",
  "tool-result-dispatcher.ts"
];

const codeRoots = [
  "apps/api/src",
  "packages/agent-runtime/src",
  "scripts"
];

const bannedCodePatterns = [
  { name: "ContextSourceAdapter", pattern: /\bContextSourceAdapter\b/ },
  { name: "ToolResultAdapter", pattern: /\bToolResultAdapter\b/ },
  { name: "ToolResultDispatcher", pattern: /\bToolResultDispatcher\b/ },
  { name: "ContextOrchestrator", pattern: /\bContextOrchestrator\b/ },
  { name: "ContextPlanner", pattern: /\bContextPlanner\b/ },
  { name: "ContextProjection type", pattern: /\bContextProjection\b(?!Builder|Input)/ },
  { name: "MastraConversationMessageContextSource", pattern: /\bMastraConversationMessageContextSource\b/ },
  { name: "MastraConversationMessageContextSourceOptions", pattern: /\bMastraConversationMessageContextSourceOptions\b/ },
  { name: "createMastraConversationMessageContextItems", pattern: /\bcreateMastraConversationMessageContextItems\b/ },
  { name: "unprefixed ContextBudgetProcessor", pattern: /\bContextBudgetProcessor\b/ },
  { name: "unprefixed ContextRuntimeSourceProcessor", pattern: /\bContextRuntimeSourceProcessor\b/ },
  { name: "unprefixed ProviderPromptGuardProcessor", pattern: /\bProviderPromptGuardProcessor\b/ },
  { name: "unprefixed TaskStateContextProcessor", pattern: /\bTaskStateContextProcessor\b/ },
  { name: "StepContextPlanner", pattern: /\bStepContextPlanner\b/ },
  { name: "bare PromptView", pattern: /(?<!Context)\bPromptView\b/ },
  { name: "bare TokenCounter", pattern: /(?<!Context)(?<!Prompt)\bTokenCounter\b/ },
  { name: "packageToolResult", pattern: /\bpackageToolResult\b/ },
  { name: "legacy context/adapters import", pattern: /context\/adapters/ },
  { name: "legacy context/defaults import", pattern: /context\/defaults/ },
  { name: "legacy context root processor import", pattern: /context\/context-budget-processor/ },
  { name: "legacy task-state processor import", pattern: /context\/task-state-context-processor/ },
  { name: "legacy mastra utils import", pattern: /context\/mastra-message-utils/ },
  { name: "legacy context policy import", pattern: /context\/context-policy/ },
  { name: "legacy context allocator import", pattern: /context\/context-budget-allocator/ },
  { name: "legacy reduction import", pattern: /context\/context-reduction-strategy/ },
  { name: "legacy model profile import", pattern: /context\/model-context-profile/ },
  { name: "legacy prompt counter import", pattern: /context\/prompt-token-counter/ },
  { name: "legacy provider guard import", pattern: /context\/provider-prompt-guard-processor/ },
  { name: "legacy root tool observation router import", pattern: /context\/tool-observation-router/ },
  { name: "legacy tool observation router import", pattern: /context\/tool-observation\/tool-observation-router/ },
  { name: "legacy tool result dispatcher import", pattern: /context\/tool-result-dispatcher/ },
  { name: "legacy tool result adapter import", pattern: /context\/tool-result-adapter/ },
  { name: "legacy schema context adapter import", pattern: /schema-context-adapter/ },
  { name: "legacy SQL context adapter import", pattern: /sql-result-context-adapter/ },
  { name: "contextPackageToItems", pattern: /\bcontextPackageToItems\b/ }
];

const bannedDocPatterns = [
  { name: "Planning Layer", pattern: /\bPlanning Layer\b/i },
  { name: "context/planning", pattern: /context\/planning/ },
  { name: "MemorySourceAdapter", pattern: /\bMemorySourceAdapter\b/ },
  { name: "Mastra Memory Adapter", pattern: /\bMastra Memory Adapter\b/ },
  { name: "KnowledgeSourceAdapter", pattern: /\bKnowledgeSourceAdapter\b/ },
  { name: "Knowledge Source Adapter", pattern: /\bKnowledge Source Adapter\b/ },
  { name: "MastraConversationMessageContextSource", pattern: /\bMastraConversationMessageContextSource\b/ },
  { name: "SystemInstructionAdapter", pattern: /\bSystemInstructionAdapter\b/ },
  { name: "ToolSchemaAdapter", pattern: /\bToolSchemaAdapter\b/ },
  { name: "ToolObservationPackage", pattern: /\bToolObservationPackage\b/ },
  { name: "ContextAdapters alias", pattern: /\bContextAdapters\b/ }
];

const failures = [];

assertContextRootHasOnlyDirectories();
assertRequiredConceptualLayerDirs();
assertRequiredSourceSubdomainDirs();
assertRequiredFiles();
assertLegacyPathsAbsent();
assertCodePatternsAbsent();
assertInventoryLayerDoesNotDependOnUpperLayers();
assertPolicyLayerDoesNotDependOnUpperLayers();
assertProjectionLayerDoesNotDependOnPolicyOrProtocols();
assertSourcePromptMaterializationPolicyIsExplicit();
assertSourceLayerDoesNotDependOnUpperLayers();
assertRuntimeSourceBoundaryStaysInSourceLayer();
assertToolObservationLayerDoesNotDependOnProtocols();
assertGenericContextPolicyStaysToolAgnostic();
assertContextBudgetAllocatorStaysProfileDriven();
assertToolObservationBudgetProfilesStayInToolLayer();
assertDefaultToolObservationAdaptersStayInToolLayer();
assertToolObservationBoundaryDoesNotLeakInternals();
assertMastraContextProcessorAssemblyStaysInBoundary();
assertRuntimeSourceProcessorDoesNotAcceptSharedAllocator();
assertToolObservationRegistryNamingIsExplicit();
assertToolObservationAssemblyStaysInBoundary();
assertProviderNeutralLayersDoNotDependOnMastra();
assertProviderNeutralLayersDoNotDependOnAgentRuntimeTypes();
assertGenericProtocolContractsStayProtocolNeutral();
assertMastraProtocolUsesContextEventSink();
assertMastraBudgetProcessorUsesInventorySystemMessages();
assertMastraBudgetProcessorUsesProtocolAdapter();
assertMastraBudgetProcessorDelegatesCompiledEventPayload();
assertMastraConversationSourceMessageClassificationIsCentralized();
assertSourcePolicyOwnsPackageCandidateSelection();
assertContextPackageDoesNotOwnFoldedToolObservationProjection();
assertToolObservationHistoryDowngradeStaysOutOfInventory();
assertRuntimeSourceProcessorUsesNarrowRunScope();
assertMastraConversationSourceIsNotRuntimeSource();
assertPolicyCountersDoNotOwnInventoryReports();
assertPolicyPlannerDoesNotReExportInventoryPlans();
assertDocPlanningLayerAbsent();
assertRootIndexDoesNotExportContextInternals();
assertAppsDoNotImportTestingEntryPoint();

if (failures.length > 0) {
  console.error("Context architecture smoke failed:");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log("Context architecture smoke passed");

function assertContextRootHasOnlyDirectories() {
  const entries = readdirSync(contextRoot, { withFileTypes: true });
  const rootFiles = entries.filter((entry) => entry.isFile()).map((entry) => entry.name);

  if (rootFiles.length > 0) {
    failures.push(`context root must not contain files: ${rootFiles.join(", ")}`);
  }
}

function assertRequiredConceptualLayerDirs() {
  for (const layerDir of requiredConceptualLayerDirs) {
    const absolutePath = path.join(contextRoot, layerDir);
    if (!existsSync(absolutePath) || !statSync(absolutePath).isDirectory()) {
      failures.push(`missing context conceptual layer directory: ${layerDir}`);
    }
  }
}

function assertRequiredSourceSubdomainDirs() {
  for (const subdomainDir of requiredSourceSubdomainDirs) {
    const absolutePath = path.join(contextRoot, subdomainDir);
    if (!existsSync(absolutePath) || !statSync(absolutePath).isDirectory()) {
      failures.push(`missing source-side context subdomain directory: ${subdomainDir}`);
    }
  }
}

function assertRequiredFiles() {
  for (const filePath of requiredFiles) {
    const absolutePath = path.join(contextRoot, filePath);
    if (!existsSync(absolutePath) || !statSync(absolutePath).isFile()) {
      failures.push(`missing required context file: ${filePath}`);
    }
  }
}

function assertLegacyPathsAbsent() {
  for (const legacyPath of bannedLegacyPaths) {
    const absolutePath = path.join(contextRoot, legacyPath);
    if (existsSync(absolutePath)) {
      failures.push(`legacy context path must stay removed: ${legacyPath}`);
    }
  }
}

function assertCodePatternsAbsent() {
  for (const filePath of listFiles(codeRoots, [".ts", ".tsx", ".js", ".mjs"])) {
    if (filePath === import.meta.filename) {
      continue;
    }

    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of bannedCodePatterns) {
      if (rule.pattern.test(source)) {
        failures.push(`legacy code pattern "${rule.name}" found in ${relativePath}`);
      }
    }
  }
}

function assertInventoryLayerDoesNotDependOnUpperLayers() {
  const inventoryRoot = path.join(contextRoot, "inventory");
  const forbiddenPatterns = [
    { name: "policy import", pattern: /(?:\.\.\/)+policy\// },
    { name: "projection import", pattern: /(?:\.\.\/)+projection\// },
    { name: "source import", pattern: /(?:\.\.\/)+source\// },
    { name: "tool observation import", pattern: /(?:\.\.\/)+tool-observation\// },
    { name: "protocol import", pattern: /(?:\.\.\/)+protocol\// },
    { name: "tool observation concept", pattern: /tool-observation/ },
    { name: "ToolObservationProjection", pattern: /\bToolObservationProjection\b/ },
    { name: "source-specific observation naming", pattern: /\bObservation\b|\bobservation\b/ }
  ];

  for (const filePath of listFiles([inventoryRoot], [".ts"])) {
    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of forbiddenPatterns) {
      if (rule.pattern.test(source)) {
        failures.push(`inventory layer must stay foundational; found "${rule.name}" in ${relativePath}`);
      }
    }
  }
}

function assertSourceLayerDoesNotDependOnUpperLayers() {
  const sourceRoot = path.join(contextRoot, "source");
  const runtimeSourcePath = path.join(sourceRoot, "runtime-context-source.ts");
  const runtimeSourceCode = readFileSync(runtimeSourcePath, "utf8");
  const workingMemoryPath = path.join(sourceRoot, "working-memory-projection-context-source.ts");
  const workingMemorySource = readFileSync(workingMemoryPath, "utf8");
  const forbiddenPatterns = [
    { name: "@mastra", pattern: /@mastra\// },
    { name: "MastraDBMessage", pattern: /\bMastraDBMessage\b/ },
    { name: "Mastra processor", pattern: /\bProcessInputStep|\bProcessor</ },
    { name: "policy import", pattern: /(?:\.\.\/)+policy\// },
    { name: "projection import", pattern: /(?:\.\.\/)+projection\// },
    { name: "tool observation import", pattern: /(?:\.\.\/)+tool-observation\// },
    { name: "context protocol import", pattern: /(?:\.\.\/)+protocol\// }
  ];

  for (const filePath of listFiles([sourceRoot], [".ts"])) {
    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of forbiddenPatterns) {
      if (rule.pattern.test(source)) {
        failures.push(`source layer must stay protocol-free; found "${rule.name}" in ${relativePath}`);
      }
    }
  }

  if (!/export\s+interface\s+RuntimeContextSource/.test(runtimeSourceCode)) {
    failures.push("source layer must expose RuntimeContextSource as an interface");
  }

  if (!/export\s+interface\s+WorkingMemoryProjectionReader/.test(workingMemorySource)) {
    failures.push("source layer extension contracts such as WorkingMemoryProjectionReader must be interfaces");
  }
}

function assertPolicyLayerDoesNotDependOnUpperLayers() {
  const policyRoot = path.join(contextRoot, "policy");
  const forbiddenPatterns = [
    { name: "projection import", pattern: /(?:\.\.\/)+projection\// },
    { name: "source import", pattern: /(?:\.\.\/)+source\// },
    { name: "tool observation import", pattern: /(?:\.\.\/)+tool-observation\// },
    { name: "protocol import", pattern: /(?:\.\.\/)+protocol\// }
  ];

  for (const filePath of listFiles([policyRoot], [".ts"])) {
    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of forbiddenPatterns) {
      if (rule.pattern.test(source)) {
        failures.push(`policy layer must not depend on upper layers; found "${rule.name}" in ${relativePath}`);
      }
    }
  }
}

function assertProjectionLayerDoesNotDependOnPolicyOrProtocols() {
  const projectionRoot = path.join(contextRoot, "projection");
  const forbiddenPatterns = [
    { name: "policy import", pattern: /(?:\.\.\/)+policy\// },
    { name: "source import", pattern: /(?:\.\.\/)+source\// },
    { name: "tool observation import", pattern: /(?:\.\.\/)+tool-observation\// },
    { name: "protocol import", pattern: /(?:\.\.\/)+protocol\// },
    { name: "source policy import", pattern: /context-source-policy/ }
  ];

  for (const filePath of listFiles([projectionRoot], [".ts"])) {
    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of forbiddenPatterns) {
      if (rule.pattern.test(source)) {
        failures.push(`projection layer must not depend on policy/protocol layers; found "${rule.name}" in ${relativePath}`);
      }
    }
  }
}

function assertSourcePromptMaterializationPolicyIsExplicit() {
  const promptMaterializerPath = path.join(contextRoot, "projection/context-prompt-materializer.ts");
  const promptMaterializerSource = readFileSync(promptMaterializerPath, "utf8");
  const sourceMaterializerPath = path.join(contextRoot, "projection/context-source-prompt-materializer.ts");
  const sourceMaterializerSource = readFileSync(sourceMaterializerPath, "utf8");
  const processorBoundaryPath = path.join(contextRoot, "protocol/mastra/mastra-context-processor-boundary.ts");
  const processorBoundarySource = readFileSync(processorBoundaryPath, "utf8");

  if (!/ContextSourcePromptMaterializer/.test(promptMaterializerSource)) {
    failures.push("ContextPromptMaterializer must delegate source prompt construction to ContextSourcePromptMaterializer");
  }

  if (/role:\s*"user"/.test(promptMaterializerSource) || /id:\s*`context:\$\{groupId\}`/.test(promptMaterializerSource)) {
    failures.push("ContextPromptMaterializer must not hard-code source prompt message shape");
  }

  if (
    !/export\s+interface\s+ContextSourcePromptMaterializer/.test(sourceMaterializerSource) ||
    !/class\s+ContextDefaultSourcePromptMaterializer/.test(sourceMaterializerSource) ||
    !/role\s*=\s*options\.role\s*\?\?\s*"user"/.test(sourceMaterializerSource)
  ) {
    failures.push("projection layer must expose an explicit default source prompt materializer");
  }

  if (
    !/sourceMaterializer\?:\s*ContextSourcePromptMaterializer/.test(processorBoundarySource) ||
    !/new\s+ContextPromptMaterializer\(\{[\s\S]*sourceMaterializer:\s*input\.contextCompilation\.sourceMaterializer/.test(
      processorBoundarySource
    )
  ) {
    failures.push("Mastra context processor boundary must pass configured source materializer into projection");
  }
}

function assertRuntimeSourceBoundaryStaysInSourceLayer() {
  const agentPath = path.join(repoRoot, "packages/agent-runtime/src/index.ts");
  const agentSource = readFileSync(agentPath, "utf8");
  const createDataFoundryBody = agentSource.slice(agentSource.indexOf("export const createDataFoundry"));
  const boundaryPath = path.join(contextRoot, "source/runtime-context-source-boundary.ts");
  const boundarySource = readFileSync(boundaryPath, "utf8");
  const processorBoundaryPath = path.join(contextRoot, "protocol/mastra/mastra-context-processor-boundary.ts");
  const processorBoundarySource = readFileSync(processorBoundaryPath, "utf8");

  if (
    /new\s+RuntimeContextSourceRegistry\(/.test(createDataFoundryBody) ||
    /new\s+LongTermMemoryContextSource\(/.test(createDataFoundryBody) ||
    /new\s+WorkingMemoryProjectionContextSource\(/.test(createDataFoundryBody) ||
    /runtimeSourceRegistry\.register/.test(createDataFoundryBody)
  ) {
    failures.push("createDataFoundry must delegate runtime source registry assembly to the source layer");
  }

  if (!/createDefaultRuntimeContextSourceRegistry/.test(processorBoundarySource)) {
    failures.push("Mastra context processor boundary must call the source-layer runtime registry boundary");
  }

  if (!/additionalSources\?:\s*RuntimeContextSource\[\]/.test(boundarySource)) {
    failures.push("source-layer runtime registry boundary must expose additionalSources for future runtime sources");
  }

  if (!/input\.additionalSources\?\.forEach\(\(source\)\s*=>\s*registry\.register\(source\)\)/.test(boundarySource)) {
    failures.push("source-layer runtime registry boundary must register additional runtime sources");
  }

  if (
    !/additionalRuntimeSources\?:\s*CreateDefaultRuntimeContextSourceRegistryInput\["additionalSources"\]/.test(
      processorBoundarySource
    ) ||
    !/additionalSources:\s*input\.additionalRuntimeSources/.test(processorBoundarySource)
  ) {
    failures.push("Mastra context processor boundary must pass additional runtime sources through source boundary");
  }

  const createDataFoundryInputBlock = agentSource.slice(
    agentSource.indexOf("export type CreateDataFoundryInput"),
    agentSource.indexOf("export const createDataFoundry")
  );

  if (/additionalRuntimeSources\?:/.test(createDataFoundryInputBlock)) {
    failures.push("createDataFoundry public input must not expose runtime source internals");
  }

  if (/additionalToolAdapters\?:|ToolObservationAdapter/.test(createDataFoundryInputBlock)) {
    failures.push("createDataFoundry public input must not expose tool-observation adapter internals");
  }

  if (
    !/createDefaultRuntimeContextSourceRegistry/.test(boundarySource) ||
    !/LongTermMemoryContextSource/.test(boundarySource) ||
    !/WorkingMemoryProjectionContextSource/.test(boundarySource)
  ) {
    failures.push("source layer must own default runtime source registry assembly");
  }
}

function assertToolObservationLayerDoesNotDependOnProtocols() {
  const toolObservationRoot = path.join(contextRoot, "tool-observation");
  const forbiddenPatterns = [
    { name: "source import", pattern: /(?:\.\.\/)+source\// },
    { name: "projection import", pattern: /(?:\.\.\/)+projection\// },
    { name: "protocol import", pattern: /(?:\.\.\/)+protocol\// }
  ];
  const policyCoordinatorPaths = new Set([
    "packages/agent-runtime/src/context/tool-observation/tool-observation-boundary.ts",
    "packages/agent-runtime/src/context/tool-observation/tool-observation-packager.ts"
  ]);
  const policyImportPattern = /(?:\.\.\/)+policy\//;

  for (const filePath of listFiles([toolObservationRoot], [".ts"])) {
    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of forbiddenPatterns) {
      if (rule.pattern.test(source)) {
        failures.push(`tool-observation layer must not depend on source/projection/protocol; found "${rule.name}" in ${relativePath}`);
      }
    }

    if (policyImportPattern.test(source) && !policyCoordinatorPaths.has(relativePath)) {
      failures.push(
        "tool-observation adapters/projection must not depend on policy; " +
          `only tool-observation boundary coordinators may import policy, found in ${relativePath}`
      );
    }
  }
}

function assertGenericContextPolicyStaysToolAgnostic() {
  const filePath = path.join(contextRoot, "policy/context-policy.ts");
  const source = readFileSync(filePath, "utf8");
  const forbiddenPatterns = [
    { name: "data gateway dependency", pattern: /@datafoundry\/data-gateway/ },
    { name: "schema projection policy", pattern: /applySchemaContextPolicy|projectSchemaToolObservation/ },
    { name: "SQL projection policy", pattern: /applySqlModelContextPolicy|projectSqlToolObservation/ },
    { name: "tool observation projection policy", pattern: /ToolObservationProjectionPolicy/ },
    { name: "agent context policy alias", pattern: /AgentContextPolicy/ }
  ];

  for (const rule of forbiddenPatterns) {
    if (rule.pattern.test(source)) {
      failures.push(`generic ContextPolicy must stay tool-agnostic; found "${rule.name}"`);
    }
  }
}

function assertContextBudgetAllocatorStaysProfileDriven() {
  const filePath = path.join(contextRoot, "policy/context-budget-allocator.ts");
  const source = readFileSync(filePath, "utf8");
  const forbiddenPatterns = [
    { name: "schema tool name", pattern: /inspect_schema/ },
    { name: "SQL tool name", pattern: /run_sql_readonly/ },
    { name: "schema limits", pattern: /SCHEMA_MAX_/ },
    { name: "SQL limits", pattern: /SQL_MAX_/ }
  ];

  for (const rule of forbiddenPatterns) {
    if (rule.pattern.test(source)) {
      failures.push(`ContextBudgetAllocator must stay profile-driven; found "${rule.name}"`);
    }
  }
}

function assertToolObservationBudgetProfilesStayInToolLayer() {
  const agentPath = path.join(repoRoot, "packages/agent-runtime/src/index.ts");
  const agentSource = readFileSync(agentPath, "utf8");
  const profilePath = path.join(contextRoot, "tool-observation/tool-observation-budget-profile.ts");
  const profileSource = readFileSync(profilePath, "utf8");

  if (/sourceLimitProfiles:\s*\{/.test(agentSource)) {
    failures.push("createDataFoundry must not inline tool observation source limit profiles");
  }

  if (
    !/DEFAULT_TOOL_OBSERVATION_SOURCE_LIMIT_PROFILES/.test(profileSource) ||
    !/inspect_schema/.test(profileSource) ||
    !/run_sql_readonly/.test(profileSource)
  ) {
    failures.push("tool-observation layer must own default tool source limit profiles");
  }
}

function assertDefaultToolObservationAdaptersStayInToolLayer() {
  const agentPath = path.join(repoRoot, "packages/agent-runtime/src/index.ts");
  const agentSource = readFileSync(agentPath, "utf8");
  const defaultAdaptersPath = path.join(contextRoot, "tool-observation/default-tool-observation-adapters.ts");
  const defaultAdaptersSource = readFileSync(defaultAdaptersPath, "utf8");
  const createDataFoundryBody = agentSource.slice(agentSource.indexOf("export const createDataFoundry"));

  if (
    /new\s+\w+ToolObservationAdapter\(/.test(createDataFoundryBody) ||
    /toolObservationRegistry\.register\(new\s+/.test(createDataFoundryBody) ||
    /registerDefaultToolObservationAdapters/.test(createDataFoundryBody) ||
    !/createToolObservationBoundary/.test(createDataFoundryBody)
  ) {
    failures.push("createDataFoundry must delegate tool observation boundary assembly");
  }

  if (
    !/SchemaToolObservationAdapter/.test(defaultAdaptersSource) ||
    !/SqlResultToolObservationAdapter/.test(defaultAdaptersSource) ||
    !/RetrieveKnowledgeToolObservationAdapter/.test(defaultAdaptersSource) ||
    !/additionalAdapters/.test(defaultAdaptersSource)
  ) {
    failures.push("tool-observation layer must own default adapter registration");
  }
}

function assertToolObservationBoundaryDoesNotLeakInternals() {
  const agentPath = path.join(repoRoot, "packages/agent-runtime/src/index.ts");
  const agentSource = readFileSync(agentPath, "utf8");
  const boundaryPath = path.join(contextRoot, "tool-observation/tool-observation-boundary.ts");
  const boundarySource = readFileSync(boundaryPath, "utf8");

  if (/toolObservationBoundary\.(budgetAllocator|registry)/.test(agentSource)) {
    failures.push("agent assembly must not reach into tool-observation boundary internals");
  }

  if (/budgetAllocator:\s*ContextBudgetAllocator/.test(boundarySource)) {
    failures.push("ToolObservationBoundary must not expose its tool-specific budget allocator");
  }

  if (/registry:\s*ToolObservationAdapterRegistry/.test(boundarySource)) {
    failures.push("ToolObservationBoundary must not expose its adapter registry");
  }
}

function assertMastraContextProcessorAssemblyStaysInBoundary() {
  const agentPath = path.join(repoRoot, "packages/agent-runtime/src/index.ts");
  const agentSource = readFileSync(agentPath, "utf8");
  const createDataFoundryBody = agentSource.slice(agentSource.indexOf("export const createDataFoundry"));
  const processorBoundaryPath = path.join(contextRoot, "protocol/mastra/mastra-context-processor-boundary.ts");
  const processorBoundarySource = readFileSync(processorBoundaryPath, "utf8");
  const forbiddenInAgent = [
    { name: "MastraContextBudgetProcessor", pattern: /new\s+MastraContextBudgetProcessor\(/ },
    { name: "MastraContextRuntimeSourceProcessor", pattern: /new\s+MastraContextRuntimeSourceProcessor\(/ },
    { name: "MastraProviderPromptGuardProcessor", pattern: /new\s+MastraProviderPromptGuardProcessor\(/ },
    { name: "MastraTaskStateContextProcessor", pattern: /new\s+MastraTaskStateContextProcessor\(/ },
    { name: "MastraToolObservationRouter", pattern: /new\s+MastraToolObservationRouter\(/ },
    { name: "ContextPromptMaterializer", pattern: /new\s+ContextPromptMaterializer\(/ },
    { name: "ContextStepPlanner", pattern: /new\s+ContextStepPlanner\(/ },
    { name: "ModelContextProfileRegistry", pattern: /new\s+ModelContextProfileRegistry\(/ },
    { name: "PromptTokenCounter", pattern: /new\s+PromptTokenCounter\(/ },
    { name: "createDefaultContextSourcePolicy", pattern: /createDefaultContextSourcePolicy\(/ },
    { name: "createDefaultRuntimeContextSourceRegistry", pattern: /createDefaultRuntimeContextSourceRegistry\(/ }
  ];
  const requiredInBoundary = [
    "createMastraContextProcessorBoundary",
    "MastraContextBudgetProcessor",
    "MastraContextRuntimeSourceProcessor",
    "MastraProviderPromptGuardProcessor",
    "MastraTaskStateContextProcessor",
    "MastraToolObservationRouter",
    "ContextStepPlanner",
    "ContextPromptMaterializer",
    "createDefaultContextSourcePolicy"
  ];

  if (!/createMastraContextProcessorBoundary\(/.test(createDataFoundryBody)) {
    failures.push("createDataFoundry must delegate Mastra context processor assembly to the protocol boundary");
  }

  for (const rule of forbiddenInAgent) {
    if (rule.pattern.test(createDataFoundryBody)) {
      failures.push(`createDataFoundry must not assemble ${rule.name} directly`);
    }
  }

  for (const symbol of requiredInBoundary) {
    if (!processorBoundarySource.includes(symbol)) {
      failures.push(`Mastra context processor boundary must own ${symbol}`);
    }
  }
}

function assertRuntimeSourceProcessorDoesNotAcceptSharedAllocator() {
  const processorPath = path.join(contextRoot, "protocol/mastra/mastra-context-runtime-source-processor.ts");
  const processorSource = readFileSync(processorPath, "utf8");
  const agentPath = path.join(repoRoot, "packages/agent-runtime/src/index.ts");
  const agentSource = readFileSync(agentPath, "utf8");

  if (/budgetAllocator\?:\s*ContextBudgetAllocator/.test(processorSource)) {
    failures.push("runtime source processor must accept budget options, not shared allocator instances");
  }

  if (/new\s+MastraContextRuntimeSourceProcessor\(\{[\s\S]*budgetAllocator:/m.test(agentSource)) {
    failures.push("agent assembly must not inject shared allocators into runtime source processors");
  }
}

function assertToolObservationRegistryNamingIsExplicit() {
  for (const filePath of listFiles(codeRoots, [".ts", ".tsx", ".js", ".mjs"])) {
    if (filePath === import.meta.filename) {
      continue;
    }

    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    if (
      /(?:const|let|var)\s+sourceRegistry\s*=\s*new\s+ToolObservationAdapterRegistry\(/.test(source) ||
      /new\s+ToolObservationPackager\([^)]*\bsourceRegistry\b/.test(source)
    ) {
      failures.push(
        `ToolObservationAdapterRegistry variables must not be named sourceRegistry in ${relativePath}`
      );
    }
  }
}

function assertToolObservationAssemblyStaysInBoundary() {
  const allowedFiles = new Set([
    "packages/agent-runtime/src/context/tool-observation/tool-observation-boundary.ts",
    "packages/agent-runtime/src/context/protocol/mastra/mastra-context-runtime-source-processor.ts"
  ]);
  const rules = [
    {
      name: "ToolObservationPackager construction",
      pattern: /new\s+ToolObservationPackager\(/
    },
    {
      name: "ToolObservationAdapterRegistry construction",
      pattern: /new\s+ToolObservationAdapterRegistry\(/
    },
    {
      name: "ContextPolicy construction",
      pattern: /new\s+ContextPolicy\(/
    },
    {
      name: "ContextBudgetAllocator construction",
      pattern: /new\s+ContextBudgetAllocator\(/
    }
  ];

  for (const filePath of listFiles(codeRoots, [".ts", ".tsx", ".js", ".mjs"])) {
    if (filePath === import.meta.filename) {
      continue;
    }

    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of rules) {
      if (!rule.pattern.test(source)) {
        continue;
      }

      const isRuntimeAllocator =
        rule.name === "ContextBudgetAllocator construction"
        && relativePath === "packages/agent-runtime/src/context/protocol/mastra/mastra-context-runtime-source-processor.ts";
      const isAllowed = allowedFiles.has(relativePath) && (
        relativePath !== "packages/agent-runtime/src/context/protocol/mastra/mastra-context-runtime-source-processor.ts"
        || isRuntimeAllocator
      );

      if (!isAllowed) {
        failures.push(`${rule.name} must stay inside context boundary assembly, found in ${relativePath}`);
      }
    }
  }
}

function assertProviderNeutralLayersDoNotDependOnMastra() {
  const providerNeutralRoots = [
    path.join(contextRoot, "inventory"),
    path.join(contextRoot, "policy"),
    path.join(contextRoot, "projection"),
    path.join(contextRoot, "source"),
    path.join(contextRoot, "tool-observation")
  ];
  const forbiddenPatterns = [
    { name: "@mastra", pattern: /@mastra\// },
    { name: "MastraDBMessage", pattern: /\bMastraDBMessage\b/ },
    { name: "Mastra processor", pattern: /\bProcessInputStep|\bProcessor</ }
  ];

  for (const filePath of listFiles(providerNeutralRoots, [".ts"])) {
    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of forbiddenPatterns) {
      if (rule.pattern.test(source)) {
        failures.push(
          `provider-neutral context layer must not depend on Mastra; found "${rule.name}" in ${relativePath}`
        );
      }
    }
  }
}

function assertProviderNeutralLayersDoNotDependOnAgentRuntimeTypes() {
  const providerNeutralRoots = [
    path.join(contextRoot, "inventory"),
    path.join(contextRoot, "policy"),
    path.join(contextRoot, "projection"),
    path.join(contextRoot, "source"),
    path.join(contextRoot, "tool-observation")
  ];
  const forbiddenPatterns = [
    { name: "agent runtime types import", pattern: /(?:\.\.\/)+types\.js/ },
    { name: "AgentRunContext", pattern: /\bAgentRunContext\b/ },
    { name: "AgUiEventEmitter", pattern: /\bAgUiEventEmitter\b/ }
  ];

  for (const filePath of listFiles(providerNeutralRoots, [".ts"])) {
    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of forbiddenPatterns) {
      if (rule.pattern.test(source)) {
        failures.push(
          `provider-neutral context layer must not depend on agent runtime types; found "${rule.name}" in ${relativePath}`
        );
      }
    }
  }
}

function assertMastraProtocolUsesContextEventSink() {
  const mastraProtocolRoot = path.join(contextRoot, "protocol/mastra");
  const forbiddenPatterns = [
    { name: "@ag-ui/core import", pattern: /@ag-ui\/core/ },
    { name: "agent runtime event helper import", pattern: /(?:\.\.\/)+\.\.\/events\.js|(?:\.\.\/)+events\.js/ },
    { name: "AgUiEventEmitter", pattern: /\bAgUiEventEmitter\b/ },
    { name: "createCustomEvent", pattern: /\bcreateCustomEvent\b/ }
  ];

  for (const filePath of listFiles([mastraProtocolRoot], [".ts"])) {
    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of forbiddenPatterns) {
      if (rule.pattern.test(source)) {
        failures.push(`Mastra context protocol must use ContextProtocolEventSink; found "${rule.name}" in ${relativePath}`);
      }
    }
  }
}

function assertGenericProtocolContractsStayProtocolNeutral() {
  const eventSinkPath = path.join(contextRoot, "protocol/context-protocol-event-sink.ts");
  const eventSinkSource = readFileSync(eventSinkPath, "utf8");
  const adapterPath = path.join(contextRoot, "protocol/context-protocol-adapter.ts");
  const adapterSource = readFileSync(adapterPath, "utf8");
  const forbiddenPatterns = [
    { name: "@ag-ui/core import", pattern: /@ag-ui\/core/ },
    { name: "agent runtime event helper import", pattern: /(?:\.\.\/)+events\.js/ },
    { name: "createCustomEvent", pattern: /\bcreateCustomEvent\b/ },
    { name: "MastraDBMessage", pattern: /\bMastraDBMessage\b/ },
    { name: "BaseEvent", pattern: /\bBaseEvent\b/ }
  ];

  for (const rule of forbiddenPatterns) {
    if (rule.pattern.test(eventSinkSource)) {
      failures.push(`generic ContextProtocolEventSink must stay protocol-neutral; found "${rule.name}"`);
    }
    if (rule.pattern.test(adapterSource)) {
      failures.push(`generic ContextProtocolAdapter must stay protocol-neutral; found "${rule.name}"`);
    }
  }

  if (
    !/export\s+interface\s+ContextProtocolAdapter<TView,\s*TProtocol>/.test(adapterSource) ||
    !/toProtocol\(view:\s*TView\):\s*TProtocol/.test(adapterSource)
  ) {
    failures.push("generic protocol layer must define ContextProtocolAdapter<TView, TProtocol>");
  }

  if (
    !/export\s+interface\s+ContextProtocolEventSink/.test(eventSinkSource) ||
    !/emitContextEvent\(name:\s*string,\s*value:\s*unknown\):\s*void/.test(eventSinkSource)
  ) {
    failures.push("generic protocol layer must define ContextProtocolEventSink");
  }
}

function assertMastraBudgetProcessorUsesInventorySystemMessages() {
  const filePath = path.join(contextRoot, "protocol/mastra/mastra-context-budget-processor.ts");
  const source = readFileSync(filePath, "utf8");

  if (/systemMessages:\s*args\.systemMessages/.test(source)) {
    failures.push("MastraContextBudgetProcessor must project systemMessages from ContextPackage inventory");
  }
}

function assertMastraBudgetProcessorUsesProtocolAdapter() {
  const budgetProcessorPath = path.join(contextRoot, "protocol/mastra/mastra-context-budget-processor.ts");
  const budgetProcessorSource = readFileSync(budgetProcessorPath, "utf8");
  const protocolAdapterPath = path.join(contextRoot, "protocol/mastra/mastra-context-protocol-adapter.ts");
  const protocolAdapterSource = readFileSync(protocolAdapterPath, "utf8");
  const agentIndexPath = path.join(repoRoot, "packages/agent-runtime/src/index.ts");
  const agentIndexSource = readFileSync(agentIndexPath, "utf8");

  if (/toMastraDBMessages\(promptView\.messages\)/.test(budgetProcessorSource)) {
    failures.push("MastraContextBudgetProcessor must convert prompt views through MastraContextProtocolAdapter");
  }

  if (/protocolAdapter\?:\s*MastraContextProtocolAdapter/.test(budgetProcessorSource)) {
    failures.push("MastraContextBudgetProcessor options must depend on ContextProtocolAdapter, not a concrete adapter");
  }

  if (
    !/class\s+MastraContextProtocolAdapter/.test(protocolAdapterSource) ||
    !/implements\s+ContextProtocolAdapter<ContextPromptView,\s*MastraContextProtocolOutput>/.test(protocolAdapterSource) ||
    !/readonly protocol = "mastra"/.test(protocolAdapterSource) ||
    !/toProtocol\(view:\s*ContextPromptView\)/.test(protocolAdapterSource)
  ) {
    failures.push("Mastra protocol layer must provide MastraContextProtocolAdapter for ContextPromptView conversion");
  }

  if (!/protocolAdapter/.test(budgetProcessorSource) || !/\.toProtocol\(promptView\)/.test(budgetProcessorSource)) {
    failures.push("MastraContextBudgetProcessor must use its configured protocol adapter");
  }

  if (/toContextPromptMessage|toMastraDBMessage|toMastraDBMessages/.test(agentIndexSource)) {
    failures.push("agent-runtime public index must not export low-level Mastra message mapper helpers");
  }
}

function assertMastraBudgetProcessorDelegatesCompiledEventPayload() {
  const filePath = path.join(contextRoot, "protocol/mastra/mastra-context-budget-processor.ts");
  const source = readFileSync(filePath, "utf8");
  const compiledEventPath = path.join(contextRoot, "protocol/mastra/mastra-context-compiled-event.ts");
  const compiledEventSource = readFileSync(compiledEventPath, "utf8");
  const planPath = path.join(contextRoot, "inventory/context-plan.ts");
  const planSource = readFileSync(planPath, "utf8");

  if (
    /selected_sources|omitted_sources|createSourceEventEntries|sourcePolicyDecisionsToContextDecisions\s*=/.test(source) ||
    !/createMastraContextCompiledEventPayload/.test(source)
  ) {
    failures.push("MastraContextBudgetProcessor must delegate context.compiled payload construction");
  }

  if (
    !/selectedSourceItemIds:\s*string\[\]/.test(planSource) ||
    !/omittedSourceItemIds:\s*string\[\]/.test(planSource) ||
    !/affectedItemIds\?:\s*string\[\]/.test(planSource) ||
    !/plan\.selectedSourceItemIds/.test(compiledEventSource) ||
    !/plan\.omittedSourceItemIds/.test(compiledEventSource) ||
    !/affectedItemIds:\s*decision\.affectedItemIds/.test(compiledEventSource)
  ) {
    failures.push("context.compiled source entries and decisions must preserve ContextPlan source item ids");
  }

  if (
    !/isContextSourceOmissionDecision/.test(source) ||
    /omittedSourceItemIds:\s*sourcePolicyResult\.decisions\.flatMap/.test(source)
  ) {
    failures.push("MastraContextBudgetProcessor must derive omitted source items only from omission decisions");
  }
}

function assertMastraConversationSourceMessageClassificationIsCentralized() {
  const conversationPath = path.join(contextRoot, "protocol/mastra/mastra-conversation-context-adapter.ts");
  const conversationSource = readFileSync(conversationPath, "utf8");
  const classifierPath = path.join(contextRoot, "protocol/mastra/mastra-context-source-message.ts");
  const classifierSource = readFileSync(classifierPath, "utf8");

  if (
    /memory-summary:/.test(conversationSource) ||
    /context:long-term-memory/.test(conversationSource) ||
    /metadata-summary/.test(conversationSource) ||
    /metadata-ltm/.test(conversationSource)
  ) {
    failures.push("MastraConversationContextAdapter must delegate context source message classification");
  }

  if (
    !/classifyMastraContextSourceMessage/.test(conversationSource) ||
    !/memory-summary:/.test(classifierSource) ||
    !/context:long-term-memory/.test(classifierSource) ||
    !/metadata-summary/.test(classifierSource) ||
    !/metadata-ltm/.test(classifierSource)
  ) {
    failures.push("Mastra context source message classifier must own compact/LTM source message rules");
  }

  if (
    !/message\.id\s*===\s*"context:long-term-memory"[\s\S]*messageKind:\s*"source-message"/.test(classifierSource)
  ) {
    failures.push("Mastra context source classifier must treat protocol-carried LTM as a governed source message");
  }
}

function assertSourcePolicyOwnsPackageCandidateSelection() {
  const processorPath = path.join(contextRoot, "protocol/mastra/mastra-context-budget-processor.ts");
  const processorSource = readFileSync(processorPath, "utf8");
  const policyPath = path.join(contextRoot, "policy/context-source-policy.ts");
  const policySource = readFileSync(policyPath, "utf8");

  if (/sourceItemsForPolicy/.test(processorSource) || !/\.applyPackage\(contextPackage\)/.test(processorSource)) {
    failures.push("MastraContextBudgetProcessor must delegate source candidate selection to ContextSourcePolicy");
  }

  if (!/applyPackage\(contextPackage:\s*ContextPackage\)/.test(policySource)) {
    failures.push("ContextSourcePolicy must own ContextPackage candidate selection");
  }

  if (/\n\s{2}apply\(items:\s*ContextItem\[\]\)/.test(policySource)) {
    failures.push("ContextSourcePolicy must not expose raw item application as a public bypass");
  }

  if (/context-source-authority-profile|DEFAULT_CONTEXT_SOURCE_AUTHORITY_ORDER|withDefaultAuthorityOrder/.test(policySource)) {
    failures.push("ContextSourcePolicy algorithm must not import or construct default source authority profiles");
  }

  const sourceSpecificPatterns = [
    { name: "compact memory source", pattern: /compact-conversation-memory/ },
    { name: "metadata summary owner", pattern: /metadata-summary/ },
    { name: "Mastra working memory owner", pattern: /mastra-working-memory/ },
    { name: "long-term memory source", pattern: /long-term-memory/ },
    { name: "metadata LTM owner", pattern: /metadata-ltm/ }
  ];

  for (const rule of sourceSpecificPatterns) {
    if (rule.pattern.test(policySource)) {
      failures.push(`ContextSourcePolicy algorithm must not hard-code source authority profiles; found "${rule.name}"`);
    }
  }
}

function assertContextPackageDoesNotOwnFoldedToolObservationProjection() {
  const filePath = path.join(contextRoot, "inventory/context-package.ts");
  const source = readFileSync(filePath, "utf8");

  if (/\bmodel:\s*unknown\b/.test(source) || /\bactivity:\s*unknown\b/.test(source)) {
    failures.push("ContextPackage must not own folded model/activity projections");
  }
}

function assertToolObservationHistoryDowngradeStaysOutOfInventory() {
  const runStatePath = path.join(contextRoot, "inventory/context-run-state.ts");
  const runStateSource = readFileSync(runStatePath, "utf8");

  if (/groupKind\s*===\s*"tool-exchange"/.test(runStateSource) || /modelObservation/.test(runStateSource)) {
    failures.push("ContextRunState must not own tool-observation history downgrade policy");
  }

  const projectionItemsPath = path.join(contextRoot, "tool-observation/tool-observation-projection-items.ts");
  const projectionItemsSource = readFileSync(projectionItemsPath, "utf8");
  const historyPath = path.join(contextRoot, "tool-observation/tool-observation-history.ts");
  const historySource = readFileSync(historyPath, "utf8");
  const packagerPath = path.join(contextRoot, "tool-observation/tool-observation-packager.ts");
  const packagerSource = readFileSync(packagerPath, "utf8");

  if (/toolObservationHistoryItemsFromPackage/.test(projectionItemsSource)) {
    failures.push("tool-observation projection items must not own history registration shaping");
  }

  if (
    !/toolObservationHistoryItemsFromPackage/.test(historySource) ||
    !/item\.visibility\s*!==\s*"model"/.test(historySource) ||
    !/visibility:\s*"reference"/.test(historySource) ||
    !/retention:\s*"reference"/.test(historySource)
  ) {
    failures.push("tool-observation layer must downgrade all model-visible tool history to reference inventory");
  }

  if (!/toolObservationHistoryItemsFromPackage/.test(packagerSource) || !/registerPackage/.test(packagerSource)) {
    failures.push("ToolObservationPackager must register downgraded tool observation history with ContextRunState");
  }
}

function assertRuntimeSourceProcessorUsesNarrowRunScope() {
  const filePath = path.join(contextRoot, "protocol/mastra/mastra-context-runtime-source-processor.ts");
  const source = readFileSync(filePath, "utf8");

  if (/\bAgentRunContext\b/.test(source) || /runContext:\s*AgentRunContext/.test(source)) {
    failures.push("MastraContextRuntimeSourceProcessor must use RuntimeContextRunScope, not full AgentRunContext");
  }

  if (!/replaceSourceItems/.test(source) || /items\.length\s*===\s*0[\s\S]*return\s+undefined/.test(source)) {
    failures.push("MastraContextRuntimeSourceProcessor must replace runtime source snapshots even when sources return no items");
  }
}

function assertMastraConversationSourceIsNotRuntimeSource() {
  const filePath = path.join(contextRoot, "protocol/mastra/mastra-conversation-context-adapter.ts");
  const source = readFileSync(filePath, "utf8");

  if (/implements\s+RuntimeContextSource/.test(source) || /RuntimeContextSourceInput/.test(source)) {
    failures.push("MastraConversationContextAdapter must stay a protocol adapter, not a runtime source");
  }
}

function assertPolicyCountersDoNotOwnInventoryReports() {
  const filePath = path.join(contextRoot, "policy/prompt-token-counter.ts");
  const source = readFileSync(filePath, "utf8");

  if (/export\s+type\s+\{\s*PromptTokenReport/.test(source)) {
    failures.push("PromptTokenReport must be exported from inventory, not policy/prompt-token-counter.ts");
  }
}

function assertPolicyPlannerDoesNotReExportInventoryPlans() {
  const filePath = path.join(contextRoot, "policy/context-step-planner.ts");
  const source = readFileSync(filePath, "utf8");

  if (/export\s+type\s+\{\s*ContextDecision,\s*ContextPlan,\s*GlobalContextBudget/.test(source)) {
    failures.push("ContextPlan DTOs must be exported from inventory, not policy/context-step-planner.ts");
  }
}

function assertDocPlanningLayerAbsent() {
  const docsRoot = path.join(repoRoot, "docs");
  for (const filePath of listFiles([docsRoot], [".md", ".mmd", ".html", ".puml"])) {
    const source = readFileSync(filePath, "utf8");
    const relativePath = path.relative(repoRoot, filePath);

    for (const rule of bannedDocPatterns) {
      if (rule.pattern.test(source)) {
        failures.push(`unexpected standalone planning layer wording "${rule.name}" found in ${relativePath}`);
      }
    }
  }
}

function assertRootIndexDoesNotExportContextInternals() {
  const filePath = path.join(repoRoot, "packages/agent-runtime/src/index.ts");
  const source = readFileSync(filePath, "utf8");
  const forbiddenExports = [];
  const exportPattern = /export\s+(?:type\s+)?\{[\s\S]*?\}\s+from\s+"([^"]+)";/g;
  let match;

  while ((match = exportPattern.exec(source)) !== null) {
    const statement = match[0];
    const exportPath = match[1] ?? "";
    const isAllowedContextExport =
      exportPath.endsWith("/context-token-counter.js") && statement.includes("ContextTokenCounter");

    if (exportPath.includes("./context/") && !isAllowedContextExport) {
      forbiddenExports.push(statement.replace(/\s+/g, " "));
    }
  }

  if (forbiddenExports.length > 0) {
    failures.push(`root agent-runtime index must not export context internals: ${forbiddenExports.join(" | ")}`);
  }
}

function assertAppsDoNotImportTestingEntryPoint() {
  const forbiddenImports = [];

  for (const filePath of listFiles(["apps"], [".ts", ".tsx", ".js", ".mjs"])) {
    const source = readFileSync(filePath, "utf8");
    if (/agent-runtime\/(?:dist\/)?testing\.js/.test(source)) {
      forbiddenImports.push(path.relative(repoRoot, filePath));
    }
  }

  if (forbiddenImports.length > 0) {
    failures.push(`apps must not import agent-runtime testing entry point: ${forbiddenImports.join(", ")}`);
  }
}

function listFiles(roots, extensions) {
  const files = [];

  for (const root of roots) {
    const absoluteRoot = path.isAbsolute(root) ? root : path.join(repoRoot, root);
    if (!existsSync(absoluteRoot)) {
      continue;
    }
    collectFiles(absoluteRoot, extensions, files);
  }

  return files;
}

function collectFiles(directory, extensions, files) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === "dist" || entry.name.startsWith(".")) {
      continue;
    }

    const absolutePath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      collectFiles(absolutePath, extensions, files);
      continue;
    }

    if (entry.isFile() && extensions.includes(path.extname(entry.name))) {
      files.push(absolutePath);
    }
  }
}
