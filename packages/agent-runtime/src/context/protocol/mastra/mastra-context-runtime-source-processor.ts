import type { ProcessInputStepArgs, ProcessInputStepResult, Processor } from "@mastra/core/processors";

import { ContextPackageBuilder } from "../../inventory/context-package-builder.js";
import type { ContextRunState } from "../../inventory/context-run-state.js";
import {
  ContextBudgetAllocator,
  type ContextBudgetAllocatorOptions
} from "../../policy/context-budget-allocator.js";
import type { RuntimeContextRunScope } from "../../source/runtime-context-source.js";
import type { RuntimeContextSourceRegistry } from "../../source/runtime-context-source-registry.js";

export type MastraContextRuntimeSourceProcessorOptions = {
  budget?: ContextBudgetAllocatorOptions;
  registry: RuntimeContextSourceRegistry;
  runScope: RuntimeContextRunScope;
  runState: ContextRunState;
};

export class MastraContextRuntimeSourceProcessor implements Processor<"context-runtime-source"> {
  readonly id = "context-runtime-source";
  readonly name = "Context Runtime Source Processor";
  private readonly budgetAllocator: ContextBudgetAllocator;
  private readonly packageBuilder = new ContextPackageBuilder();

  constructor(private readonly options: MastraContextRuntimeSourceProcessorOptions) {
    this.budgetAllocator = new ContextBudgetAllocator(options.budget);
  }

  async processInputStep(args: ProcessInputStepArgs): Promise<ProcessInputStepResult | undefined> {
    const sources = this.options.registry.list();
    const collected = await Promise.all(sources.map((source) =>
      source.collect({
        budget: this.budgetAllocator.allocate({
          sourceType: source.sourceType
        }),
        runId: this.options.runScope.runId,
        sessionId: this.options.runScope.sessionId,
        userId: this.options.runScope.userId
      })
    ));
    const items = collected.flat();
    const contextPackage = this.packageBuilder.build(items, {
      resourceId: this.options.runScope.userId,
      runId: this.options.runScope.runId,
      sessionId: this.options.runScope.sessionId
    });

    this.options.runState.replaceSourceItems(sources.map((source) => source.sourceType), contextPackage);

    return { messages: args.messages, systemMessages: args.systemMessages };
  }
}
