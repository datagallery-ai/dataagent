// Packages source-governed tool observations into ContextPackage layers.

import type { ContextPackage } from "../inventory/context-package.js";
import { ContextPackageBuilder } from "../inventory/context-package-builder.js";
import type { ContextBudgetAllocator } from "../policy/context-budget-allocator.js";
import type { ContextPolicy } from "../policy/context-policy.js";
import type { ContextRunState } from "../inventory/context-run-state.js";
import type { ToolObservationAdapterRegistry } from "./tool-observation-adapter-registry.js";
import type { ToolObservationRunScope } from "./tool-observation-run-scope.js";
import { toolObservationHistoryItemsFromPackage } from "./tool-observation-history.js";

export type PackageToolObservationInput = {
  toolName: string;
  rawResult: unknown;
  runScope: ToolObservationRunScope;
};

export class ToolObservationPackager {
  private readonly packageBuilder = new ContextPackageBuilder();

  constructor(
    private budgetAllocator: ContextBudgetAllocator,
    private toolObservationRegistry: ToolObservationAdapterRegistry,
    private policy: ContextPolicy,
    private runState?: ContextRunState
  ) {}

  packageToolObservation(input: PackageToolObservationInput): ContextPackage {
    const adapter = this.toolObservationRegistry.resolveByToolName(input.toolName);

    if (!adapter) {
      throw new Error(`CONTEXT_ADAPTER_REQUIRED:${input.toolName}`);
    }

    const budget = this.budgetAllocator.allocate({
      sourceType: adapter.sourceType,
      toolName: input.toolName
    });
    const items = adapter.toContextItems(input.rawResult, budget);
    const governedItems = this.policy.applyBudget(this.policy.redact(items), budget, (text) =>
      this.budgetAllocator.countTokensSync(text, input.runScope.modelName)
    );
    const contextPackage = this.packageBuilder.build(governedItems, createPackageOptions(input.runScope));
    this.registerObservationHistory(contextPackage, input.runScope);
    return contextPackage;
  }

  /** Return whether a tool has an exact observation adapter registered. */
  hasToolAdapter(toolName: string): boolean {
    return this.toolObservationRegistry.resolveByToolName(toolName) !== undefined;
  }

  private registerObservationHistory(
    contextPackage: ContextPackage,
    runScope: ToolObservationRunScope
  ): void {
    if (!this.runState) {
      return;
    }

    this.runState.registerPackage(this.packageBuilder.build(
      toolObservationHistoryItemsFromPackage(contextPackage),
      {
        ...createPackageOptions(runScope),
        packageId: contextPackage.packageId,
        revision: contextPackage.revision
      }
    ));
  }
}

const createPackageOptions = (runScope: ToolObservationRunScope) => ({
  resourceId: runScope.resourceId,
  sessionId: runScope.sessionId,
  runId: runScope.runId
});
