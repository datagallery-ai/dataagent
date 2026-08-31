import type { InputProcessorOrWorkflow, OutputProcessorOrWorkflow } from "@mastra/core/processors";

import type { ContextRunState } from "../../inventory/context-run-state.js";
import {
  type ReductionCandidateSelector,
  ReductionStrategyRegistry
} from "../../policy/context-reduction-strategy.js";
import { createDefaultContextSourcePolicy } from "../../policy/context-source-authority-profile.js";
import { ContextStepPlanner } from "../../policy/context-step-planner.js";
import { ModelContextProfileRegistry, type ModelContextProfile } from "../../policy/model-context-profile.js";
import { PromptTokenCounter } from "../../policy/prompt-token-counter.js";
import { ContextPromptMaterializer } from "../../projection/context-prompt-materializer.js";
import type { ContextSourcePromptMaterializer } from "../../projection/context-source-prompt-materializer.js";
import {
  createDefaultRuntimeContextSourceRegistry,
  type CreateDefaultRuntimeContextSourceRegistryInput
} from "../../source/runtime-context-source-boundary.js";
import type { RuntimeContextRunScope } from "../../source/runtime-context-source.js";
import type { ToolObservationDispatcher } from "../../tool-observation/tool-observation-dispatcher.js";
import type { TaskStateRuntime } from "../../../memory/task-state-runtime.js";
import type { ContextProtocolEventSink } from "../context-protocol-event-sink.js";
import {
  MastraContextBudgetProcessor,
  type ContextPackageRecorder
} from "./mastra-context-budget-processor.js";
import { MastraContextProtocolAdapter } from "./mastra-context-protocol-adapter.js";
import { MastraContextRuntimeSourceProcessor } from "./mastra-context-runtime-source-processor.js";
import { MastraCustomDataPartFilterProcessor } from "./mastra-custom-data-part-filter-processor.js";
import { MastraProviderPromptGuardProcessor } from "./mastra-provider-prompt-guard-processor.js";
import { MastraTaskStateContextProcessor } from "./mastra-task-state-context-processor.js";
import { MastraToolObservationRouter } from "./mastra-tool-observation-router.js";

export type MastraContextCompilationOptions = {
  candidateSelector?: ReductionCandidateSelector;
  profileRegistry?: ModelContextProfileRegistry;
  registerDefaultStrategies?: boolean;
  sourceMaterializer?: ContextSourcePromptMaterializer;
  strategyRegistry?: ReductionStrategyRegistry;
  tokenCounter?: PromptTokenCounter;
};

export type CreateMastraContextProcessorBoundaryInput = {
  additionalRuntimeSources?: CreateDefaultRuntimeContextSourceRegistryInput["additionalSources"];
  contextCompilation?: MastraContextCompilationOptions;
  contextPackageRecorder?: ContextPackageRecorder;
  dispatcher: ToolObservationDispatcher;
  eventSink: ContextProtocolEventSink;
  longTermMemory?: CreateDefaultRuntimeContextSourceRegistryInput["longTermMemory"];
  modelContextProfile?: ModelContextProfile;
  modelName: string | undefined;
  runScope: RuntimeContextRunScope;
  runState: ContextRunState;
  taskStateRuntime?: TaskStateRuntime;
};

export type MastraContextProcessorBoundary = {
  inputProcessors: InputProcessorOrWorkflow[];
  outputProcessors: OutputProcessorOrWorkflow[];
};

export const createMastraContextProcessorBoundary = (
  input: CreateMastraContextProcessorBoundaryInput
): MastraContextProcessorBoundary => {
  const profileRegistry = input.contextCompilation?.profileRegistry ?? new ModelContextProfileRegistry({
    ...(input.modelContextProfile ? { defaultProfile: input.modelContextProfile } : {})
  });
  const tokenCounter = input.contextCompilation?.tokenCounter ?? new PromptTokenCounter();
  const planner = new ContextStepPlanner({
    profileRegistry,
    tokenCounter,
    ...(input.contextCompilation?.strategyRegistry
      ? { strategyRegistry: input.contextCompilation.strategyRegistry }
      : {}),
    ...(input.contextCompilation?.candidateSelector
      ? { candidateSelector: input.contextCompilation.candidateSelector }
      : {}),
    ...(input.contextCompilation?.registerDefaultStrategies !== undefined
      ? { registerDefaultStrategies: input.contextCompilation.registerDefaultStrategies }
      : {})
  });
  const runtimeSourceRegistry = createDefaultRuntimeContextSourceRegistry({
    ...(input.additionalRuntimeSources?.length ? { additionalSources: input.additionalRuntimeSources } : {}),
    ...(input.longTermMemory?.records.length ? { longTermMemory: input.longTermMemory } : {}),
    ...(input.taskStateRuntime ? { workingMemory: input.taskStateRuntime.memory } : {})
  });
  const runtimeSourceProcessor = runtimeSourceRegistry.list().length
    ? new MastraContextRuntimeSourceProcessor({
        registry: runtimeSourceRegistry,
        runScope: input.runScope,
        runState: input.runState
      })
    : undefined;
  const taskStateContextProcessor = input.taskStateRuntime
    ? new MastraTaskStateContextProcessor({
        runtime: input.taskStateRuntime,
        threadId: input.runScope.sessionId
      })
    : undefined;
  const contextBudgetProcessor = new MastraContextBudgetProcessor({
    ...(input.contextPackageRecorder ? { contextPackageRecorder: input.contextPackageRecorder } : {}),
    eventSink: input.eventSink,
    materializer: new ContextPromptMaterializer({
      ...(input.contextCompilation?.sourceMaterializer
        ? { sourceMaterializer: input.contextCompilation.sourceMaterializer }
        : {})
    }),
    modelName: input.modelName,
    planner,
    protocolAdapter: new MastraContextProtocolAdapter(),
    runState: input.runState,
    sourcePolicy: createDefaultContextSourcePolicy(),
    toolObservationRouter: new MastraToolObservationRouter({
      dispatcher: input.dispatcher,
      eventSink: input.eventSink
    })
  });
  const providerPromptGuard = new MastraProviderPromptGuardProcessor({
    eventSink: input.eventSink,
    modelName: input.modelName,
    profileRegistry,
    tokenCounter
  });

  return {
    inputProcessors: [
      ...(taskStateContextProcessor ? [taskStateContextProcessor] : []),
      ...(runtimeSourceProcessor ? [runtimeSourceProcessor] : []),
      contextBudgetProcessor,
      providerPromptGuard
    ],
    outputProcessors: [new MastraCustomDataPartFilterProcessor()]
  };
};
