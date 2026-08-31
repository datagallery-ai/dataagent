import { describe, expect, it } from "vitest";

import { createToolCapabilityPlugin } from "./tool-capability-plugin.js";

describe("createToolCapabilityPlugin", () => {
  it("preserves the original tool invocation arguments", async () => {
    const calls: unknown[][] = [];
    const plugin = createToolCapabilityPlugin({
      id: "existing-tools",
      tools: {
        test_tool: {
          execute: async (...args: unknown[]) => {
            calls.push(args);
            return { ok: true };
          }
        }
      }
    });
    const action = plugin.actions[0];
    if (!action) {
      throw new Error("TEST_ACTION_REQUIRED");
    }

    const result = await action.execute({
      actionId: "action-1",
      actionName: "test_tool",
      runId: "run-1",
      segmentId: "segment-1",
      invocationArgs: [{ toolCallId: "call-1" }]
    }, { value: 1 });

    expect(result).toEqual({ ok: true });
    expect(calls).toEqual([[{ value: 1 }, { toolCallId: "call-1" }]]);
  });

  it("binds each existing tool to the selected protocol reducer", () => {
    const plugin = createToolCapabilityPlugin({
      id: "existing-tools",
      tools: { test_tool: { execute: async () => ({ value: 2 }) } },
      reduceAction: (state, actionName, result) => ({
        total: (state as { total: number }).total + (result as { value: number }).value,
        actionName
      })
    });
    const action = plugin.actions[0];
    if (!action?.reduce) {
      throw new Error("TEST_REDUCER_REQUIRED");
    }

    expect(action.reduce({ total: 1 }, { value: 2 })).toEqual({ total: 3, actionName: "test_tool" });
  });
});
