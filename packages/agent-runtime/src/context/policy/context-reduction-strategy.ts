import type { ContextRetention } from "../inventory/context-item.js";

export type ReductionGroup = {
  id: string;
  retention: ContextRetention;
  mandatory: boolean;
  order: number;
  tokenCost: number;
};

export type ReductionState = {
  groups: ReductionGroup[];
  selectedGroupIds: ReadonlySet<string>;
  excessTokens: number;
};

export type ReductionProposal = {
  strategyId: string;
  removeGroupIds: string[];
  expectedTokenSavings: number;
  qualityLoss: number;
  reason: string;
};

export interface ContextReductionStrategy {
  readonly id: string;
  propose(state: ReductionState): ReductionProposal[];
}

export interface ReductionCandidateSelector {
  select(proposals: ReductionProposal[], state: ReductionState): ReductionProposal | undefined;
}

export class ReductionStrategyRegistry {
  private readonly strategies: ContextReductionStrategy[] = [];

  register(strategy: ContextReductionStrategy): void {
    if (this.strategies.some((entry) => entry.id === strategy.id)) {
      throw new Error(`CONTEXT_REDUCTION_STRATEGY_ALREADY_REGISTERED:${strategy.id}`);
    }
    this.strategies.push(strategy);
  }

  propose(state: ReductionState): ReductionProposal[] {
    return this.strategies.flatMap((strategy) => strategy.propose(state));
  }
}

export class LowestQualityLossSelector implements ReductionCandidateSelector {
  select(proposals: ReductionProposal[]): ReductionProposal | undefined {
    return [...proposals].sort((left, right) =>
      left.qualityLoss - right.qualityLoss
      || right.expectedTokenSavings - left.expectedTokenSavings
      || left.strategyId.localeCompare(right.strategyId)
    )[0];
  }
}

export class OmitHistoricalGroupStrategy implements ContextReductionStrategy {
  readonly id = "omit-historical-group";

  propose(state: ReductionState): ReductionProposal[] {
    return state.groups
      .filter((group) =>
        state.selectedGroupIds.has(group.id)
        && !group.mandatory
        && group.retention === "historical"
      )
      .map((group) => ({
        strategyId: this.id,
        removeGroupIds: [group.id],
        expectedTokenSavings: group.tokenCost,
        qualityLoss: group.order + 1,
        reason: `Omitted historical context group ${group.id}`
      }));
  }
}
