import { randomUUID } from "node:crypto";

import type { ContextPackage } from "../inventory/context-package.js";
import type { ContextDecision, ContextPlan, GlobalContextBudget } from "../inventory/context-plan.js";
import {
  LowestQualityLossSelector,
  OmitHistoricalGroupStrategy,
  ReductionStrategyRegistry,
  type ReductionCandidateSelector,
  type ReductionGroup,
  type ReductionProposal
} from "./context-reduction-strategy.js";
import { ModelContextProfileRegistry, type ModelContextProfile } from "./model-context-profile.js";
import { PromptTokenCounter } from "./prompt-token-counter.js";
import type { PromptTokenReport } from "../inventory/context-token-report.js";

export type PromptMessageGroup = ReductionGroup;

export type ContextPlanningGroupInput = Omit<ReductionGroup, "tokenCost"> & {
  messages: unknown[];
};

export type ContextStepPlanResult = {
  groups: PromptMessageGroup[];
  plan: ContextPlan;
  selectedGroupIds: ReadonlySet<string>;
};

export type ContextStepPlannerOptions = {
  profileRegistry?: ModelContextProfileRegistry;
  tokenCounter?: PromptTokenCounter;
  strategyRegistry?: ReductionStrategyRegistry;
  candidateSelector?: ReductionCandidateSelector;
  registerDefaultStrategies?: boolean;
};

export class ContextStepPlanner {
  private readonly profileRegistry: ModelContextProfileRegistry;
  private readonly tokenCounter: PromptTokenCounter;
  private readonly strategyRegistry: ReductionStrategyRegistry;
  private readonly candidateSelector: ReductionCandidateSelector;

  constructor(options: ContextStepPlannerOptions = {}) {
    this.profileRegistry = options.profileRegistry ?? new ModelContextProfileRegistry();
    this.tokenCounter = options.tokenCounter ?? new PromptTokenCounter();
    this.strategyRegistry = options.strategyRegistry ?? new ReductionStrategyRegistry();
    this.candidateSelector = options.candidateSelector ?? new LowestQualityLossSelector();

    if (options.registerDefaultStrategies !== false) {
      this.strategyRegistry.register(new OmitHistoricalGroupStrategy());
    }
  }

  resolveProfile(modelName: string | undefined): ModelContextProfile {
    return this.profileRegistry.resolve(modelName);
  }

  createPlanningGroups(input: {
    groups: ContextPlanningGroupInput[];
    modelName: string | undefined;
  }): PromptMessageGroup[] {
    const profile = this.resolveProfile(input.modelName);
    return input.groups.map((group) => ({
      id: group.id,
      mandatory: group.mandatory,
      order: group.order,
      retention: group.retention,
      tokenCost: this.tokenCounter.countMessages(group.messages, input.modelName, profile)
    }));
  }

  plan(input: {
    contextPackage: ContextPackage;
    stepNumber: number;
    systemMessages: unknown[];
    tools?: Record<string, unknown>;
    modelName: string | undefined;
    groups: PromptMessageGroup[];
    sourceDecisions?: ContextDecision[];
    sourceItemIds?: string[];
    omittedSourceItemIds?: string[];
  }): ContextStepPlanResult {
    const profile = this.profileRegistry.resolve(input.modelName);
    const groups = input.groups;
    const selectedGroupIds = new Set(groups.map((group) => group.id));
    const decisions: ContextDecision[] = [...(input.sourceDecisions ?? [])];
    let report = this.createReport(input, selectedGroupIds, groups, profile);

    this.assertMandatorySetFits(input, groups, profile);

    while (report.totalInputTokens > report.inputBudget) {
      const state = {
        groups,
        selectedGroupIds,
        excessTokens: report.totalInputTokens - report.inputBudget
      };
      const proposal = this.candidateSelector.select(this.strategyRegistry.propose(state), state);
      this.applyProposal(proposal, selectedGroupIds, decisions);
      report = this.createReport(input, selectedGroupIds, groups, profile);
    }

    const plan: ContextPlan = {
      planId: randomUUID(),
      stepNumber: input.stepNumber,
      packageRevision: input.contextPackage.revision,
      selectedGroupIds: [...selectedGroupIds],
      omittedGroupIds: groups.filter((group) => !selectedGroupIds.has(group.id)).map((group) => group.id),
      selectedSourceItemIds: input.sourceItemIds ?? [],
      omittedSourceItemIds: input.omittedSourceItemIds ?? [],
      decisions,
      budget: createBudget(profile),
      tokenReport: report
    };

    return {
      plan,
      groups,
      selectedGroupIds
    };
  }

  private createReport(
    input: {
      systemMessages: unknown[];
      tools?: Record<string, unknown>;
      modelName: string | undefined;
    },
    selectedGroupIds: ReadonlySet<string>,
    groups: PromptMessageGroup[],
    profile: ModelContextProfile
  ): PromptTokenReport {
    const messageTokens = groups
      .filter((group) => selectedGroupIds.has(group.id))
      .reduce((sum, group) => sum + group.tokenCost, 0);
    return this.tokenCounter.countPrecomputedMessages({
      systemMessages: input.systemMessages,
      ...(input.tools ? { tools: input.tools } : {}),
      messageTokens,
      modelName: input.modelName,
      profile
    });
  }

  private assertMandatorySetFits(
    input: {
      systemMessages: unknown[];
      tools?: Record<string, unknown>;
      modelName: string | undefined;
    },
    groups: PromptMessageGroup[],
    profile: ModelContextProfile
  ): void {
    const messageTokens = groups
      .filter((group) => group.mandatory)
      .reduce((sum, group) => sum + group.tokenCost, 0);
    const report = this.tokenCounter.countPrecomputedMessages({
      systemMessages: input.systemMessages,
      ...(input.tools ? { tools: input.tools } : {}),
      messageTokens,
      modelName: input.modelName,
      profile
    });

    if (report.totalInputTokens > report.inputBudget) {
      throw new Error("CONTEXT_MINIMUM_SET_EXCEEDS_BUDGET");
    }
  }

  private applyProposal(
    proposal: ReductionProposal | undefined,
    selectedGroupIds: Set<string>,
    decisions: ContextDecision[]
  ): void {
    if (!proposal || proposal.removeGroupIds.every((groupId) => !selectedGroupIds.has(groupId))) {
      throw new Error("CONTEXT_REDUCTION_STRATEGY_EXHAUSTED");
    }

    proposal.removeGroupIds.forEach((groupId) => selectedGroupIds.delete(groupId));
    decisions.push({
      strategyId: proposal.strategyId,
      affectedGroupIds: proposal.removeGroupIds,
      tokenSavings: proposal.expectedTokenSavings,
      reason: proposal.reason
    });
  }
}

const createBudget = (profile: ModelContextProfile): GlobalContextBudget => ({
  contextWindow: profile.contextWindow,
  outputReserve: profile.outputReserve,
  safetyMargin: profile.safetyMargin,
  inputBudget: Math.max(profile.contextWindow - profile.outputReserve - profile.safetyMargin, 0)
});
