import type { ProcessInputStepArgs, ProcessInputStepResult, Processor } from "@mastra/core/processors";

import type { ContextPackage } from "../../inventory/context-package.js";
import { ContextPackageBuilder } from "../../inventory/context-package-builder.js";
import type { ContextItem } from "../../inventory/context-item.js";
import type { ContextPlan } from "../../inventory/context-plan.js";
import type { ContextRunState } from "../../inventory/context-run-state.js";
import { ContextSourcePolicy } from "../../policy/context-source-policy.js";
import { isContextSourceOmissionDecision } from "../../policy/context-source-policy.js";
import { createDefaultContextSourcePolicy } from "../../policy/context-source-authority-profile.js";
import { ContextStepPlanner } from "../../policy/context-step-planner.js";
import { ContextPromptMaterializer } from "../../projection/context-prompt-materializer.js";
import type { ContextPromptView } from "../../projection/context-prompt-view.js";
import type { ContextProtocolAdapter } from "../context-protocol-adapter.js";
import type { ContextProtocolEventSink } from "../context-protocol-event-sink.js";
import {
  createMastraContextCompiledEventPayload,
  sourcePolicyDecisionsToContextDecisions
} from "./mastra-context-compiled-event.js";
import {
  MastraContextProtocolAdapter,
  type MastraContextProtocolOutput
} from "./mastra-context-protocol-adapter.js";
import { createMastraConversationContextItems } from "./mastra-conversation-context-adapter.js";
import { applyMastraPromptSnapshot } from "./mastra-prompt-snapshot-applicator.js";
import type { MastraToolObservationRouter } from "./mastra-tool-observation-router.js";

export type MastraContextBudgetProcessorOptions = {
  contextPackageRecorder?: ContextPackageRecorder;
  eventSink: ContextProtocolEventSink;
  materializer?: ContextPromptMaterializer;
  modelName: string | undefined;
  planner?: ContextStepPlanner;
  protocolAdapter?: ContextProtocolAdapter<ContextPromptView, MastraContextProtocolOutput>;
  runState: ContextRunState;
  sourcePolicy?: ContextSourcePolicy;
  toolObservationRouter?: MastraToolObservationRouter;
};

export type ContextPackageRecorder = {
  record(input: { contextPackage: ContextPackage; plan?: ContextPlan }): void;
};

export class MastraContextBudgetProcessor implements Processor<"context-budget"> {
  readonly id = "context-budget";
  readonly name = "Context Budget Processor";
  private readonly builder = new ContextPackageBuilder();
  private readonly materializer: ContextPromptMaterializer;
  private readonly planner: ContextStepPlanner;
  private readonly protocolAdapter: ContextProtocolAdapter<ContextPromptView, MastraContextProtocolOutput>;
  private readonly sourcePolicy: ContextSourcePolicy;

  constructor(private readonly options: MastraContextBudgetProcessorOptions) {
    this.materializer = options.materializer ?? new ContextPromptMaterializer();
    this.planner = options.planner ?? new ContextStepPlanner();
    this.protocolAdapter = options.protocolAdapter ?? new MastraContextProtocolAdapter();
    this.sourcePolicy = options.sourcePolicy ?? createDefaultContextSourcePolicy();
  }

  processInputStep(args: ProcessInputStepArgs): ProcessInputStepResult {
    const governedMessages = this.options.toolObservationRouter?.governMessages(args.messages) ?? args.messages;
    const livePackage = this.builder.build(
      createMastraConversationContextItems(governedMessages, args.systemMessages),
      {
        resourceId: this.options.runState.identity.resourceId,
        sessionId: this.options.runState.identity.sessionId,
        runId: this.options.runState.identity.runId
      }
    );
    const contextPackage = this.options.runState.replaceMatchingItems(isProtocolMessageItem, livePackage);
    const sourcePolicyResult = this.sourcePolicy.applyPackage(contextPackage);
    const groupPlan = this.materializer.createGroupPlan({
      contextPackage,
      sourceItemIds: new Set(sourcePolicyResult.items.map((item) => item.id))
    });
    const planningGroups = this.planner.createPlanningGroups({
      groups: groupPlan.groups,
      modelName: this.options.modelName
    });
    const result = this.planner.plan({
      contextPackage,
      groups: planningGroups,
      stepNumber: args.stepNumber,
      systemMessages: groupPlan.systemMessages,
      ...(args.tools ? { tools: args.tools } : {}),
      modelName: this.options.modelName,
      sourceDecisions: sourcePolicyDecisionsToContextDecisions(sourcePolicyResult.decisions),
      sourceItemIds: sourcePolicyResult.items.map((item) => item.id),
      omittedSourceItemIds: sourcePolicyResult.decisions
        .filter(isContextSourceOmissionDecision)
        .flatMap((decision) => decision.affectedItemIds)
    });
    const promptView = this.materializer.materializePromptView({
      groups: groupPlan.groups,
      selectedGroupIds: result.selectedGroupIds,
      systemMessages: groupPlan.systemMessages,
      tokenReport: result.plan.tokenReport
    });
    this.options.runState.recordPlan(result.plan);
    this.options.contextPackageRecorder?.record({
      contextPackage,
      plan: result.plan
    });
    this.options.eventSink.emitContextEvent(
      "context.compiled",
      createMastraContextCompiledEventPayload(contextPackage, result.plan, this.options.modelName)
    );
    const protocolOutput = this.protocolAdapter.toProtocol(promptView);
    const systemMessages = protocolOutput.systemMessages as NonNullable<ProcessInputStepResult["systemMessages"]>;

    if (!args.messageList) {
      return {
        messages: protocolOutput.messages,
        systemMessages
      };
    }

    applyMastraPromptSnapshot(args.messageList, { messages: protocolOutput.messages });

    return {
      messageList: args.messageList,
      systemMessages
    };
  }
}

const PROTOCOL_MESSAGE_KINDS = new Set(["message", "source-message", "system"]);

const isProtocolMessageItem = (item: ContextItem): boolean => {
  const messageKind = item.metadata.messageKind;
  return typeof messageKind === "string" && PROTOCOL_MESSAGE_KINDS.has(messageKind);
};
