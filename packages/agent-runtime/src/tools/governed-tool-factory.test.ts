import { EventType } from "@ag-ui/core";
import { describe, expect, it } from "vitest";
import { z } from "zod";

import { ActionRouter } from "../capabilities/action-router.js";
import { CapabilityRegistry } from "../capabilities/capability-registry.js";
import { toToolExecutionError } from "../errors/tool-execution-error.js";
import { InMemoryProtocolStateStore } from "../protocol/in-memory-protocol-state-store.js";
import { ProtocolRuntime } from "../protocol/protocol-runtime.js";
import type { AgentProtocolDefinition } from "../protocol/types.js";
import { createToolObservationBoundary } from "../context/tool-observation/tool-observation-boundary.js";
import { ToolObservationDispatcher } from "../context/tool-observation/tool-observation-dispatcher.js";
import { GovernedToolFactory } from "./governed-tool-factory.js";

describe("GovernedToolFactory protocol boundary", () => {
  it("rejects a disallowed tool before its executor runs", async () => {
    let executed = false;
    const emitted: Array<{ content?: string; type?: string }> = [];
    const runScope = { modelName: "test", resourceId: "user-1", runId: "run-1", sessionId: "session-1" };
    const boundary = createToolObservationBoundary({
      additionalAdapters: [{
        toolName: "test_tool",
        resultType: "test",
        sourceType: "tool-observation",
        toContextItems: () => []
      }],
      identity: runScope
    });
    const dispatcher = new ToolObservationDispatcher(boundary.packager, runScope);
    const registry = new CapabilityRegistry();
    registry.register({
      manifest: { id: "test-tools", version: "1", provides: ["test_tool"] },
      actions: [{
        name: "test_tool",
        exposure: "agent",
        inputSchema: z.unknown(),
        outputSchema: z.unknown(),
        idempotency: "none",
        execute: async () => {
          executed = true;
          return { ok: true };
        }
      }]
    });
    const runtime = new ProtocolRuntime(createProtocol(), new InMemoryProtocolStateStore());
    runtime.start({
      runId: "run-1",
      segmentId: "segment-1",
      contextPackageRef: { packageId: "context-1", revision: 0 }
    });
    const actionRouter = new ActionRouter(registry, runtime, {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({ packageId: "context-1", revision: 1 })
    });
    const factory = new GovernedToolFactory(dispatcher, undefined, undefined, {
      actionRouter,
      emitter: { emit: (event) => emitted.push(event) },
      runId: "run-1",
      segmentId: "segment-1"
    });
    const rawTool: { execute(...args: unknown[]): Promise<unknown> } = {
      execute: async () => ({ ok: true })
    };
    const tool = factory.governTool("test_tool", rawTool);

    await expect(tool.execute?.({}, { agent: { toolCallId: "call-rejected" } }))
      .rejects.toThrow("ACTION_NOT_ALLOWED_IN_PHASE:active:test_tool");
    expect(executed).toBe(false);
    expect(emitted).toHaveLength(1);
    expect(emitted[0]?.type).toBe(EventType.TOOL_CALL_RESULT);
    expect(JSON.parse(emitted[0]?.content ?? "{}")).toEqual({
      ok: false,
      isError: true,
      error: {
        code: "ACTION_NOT_ALLOWED_IN_PHASE",
        category: "protocol",
        message: "Tool test_tool is not allowed in protocol phase active.",
        executionStatus: "not_started",
        retryable: false
      },
      recovery: {
        strategy: "refresh_and_replan",
        instruction: "Choose an action allowed in the current phase before calling this tool again.",
        avoid: ["Do not repeat test_tool while the protocol remains in phase active."]
      }
    });
  });

  it("preserves governed result callbacks after Action Router execution", async () => {
    const runScope = { modelName: "test", resourceId: "user-1", runId: "run-1", sessionId: "session-1" };
    const boundary = createToolObservationBoundary({
      additionalAdapters: [{
        toolName: "test_tool",
        resultType: "test",
        sourceType: "tool-observation",
        toContextItems: () => []
      }],
      identity: runScope
    });
    const dispatcher = new ToolObservationDispatcher(boundary.packager, runScope);
    const callbacks: unknown[] = [];
    const contextPackage = {
      version: 2 as const,
      packageId: "context-1",
      revision: 1,
      items: [],
      groups: [],
      sourceSnapshots: [],
      artifactRefs: [],
      auditRefs: [],
      truncation: []
    };
    const factory = new GovernedToolFactory(dispatcher, (result) => {
      callbacks.push(result);
    }, undefined, {
      actionRouter: {
        execute: async () => ({
          rawResult: { ok: true },
          observation: { summary: "done" },
          contextPackageRef: { packageId: "context-1", revision: 1 },
          contextPackage
        })
      },
      runId: "run-1",
      segmentId: "segment-1"
    });
    const tool = factory.governTool("test_tool", {
      execute: async (..._args: unknown[]) => ({ shouldNotRun: true })
    });

    const result = await tool.execute?.({}, { agent: { toolCallId: "call-1" } });

    expect(result).toEqual({ summary: "done" });
    expect(callbacks).toHaveLength(1);
    expect(callbacks[0]).toMatchObject({
      contextPackage,
      rawResult: { ok: true },
      toolName: "test_tool",
      toolCallId: "call-1"
    });
  });

  it("bypasses ActionRouter for externally resolved HITL tools so suspend can propagate", async () => {
    let actionRouterCalled = false;
    let suspendedPayload: unknown;
    const runScope = { modelName: "test", resourceId: "user-1", runId: "run-1", sessionId: "session-1" };
    const boundary = createToolObservationBoundary({ identity: runScope });
    const dispatcher = new ToolObservationDispatcher(boundary.packager, runScope);
    const factory = new GovernedToolFactory(dispatcher, undefined, undefined, {
      actionRouter: {
        execute: async () => {
          actionRouterCalled = true;
          return {
            rawResult: { shouldNotRun: true },
            observation: { shouldNotRun: true },
            contextPackageRef: { packageId: "context-1", revision: 1 },
            contextPackage: {
              version: 2 as const,
              packageId: "context-1",
              revision: 1,
              items: [],
              groups: [],
              sourceSnapshots: [],
              artifactRefs: [],
              auditRefs: [],
              truncation: []
            }
          };
        }
      },
      externallyResolvedToolNames: new Set(["ask_user"]),
      runId: "run-1",
      segmentId: "segment-1"
    });
    const tool = factory.governTool("ask_user", {
      execute: async (_input: unknown, options?: unknown) => {
        const agent = (options as { agent?: {
          suspend?: (payload: unknown) => Promise<void>;
        } } | undefined)?.agent;
        await agent?.suspend?.({ question: "Which datasource?" });
        return undefined;
      }
    });

    const result = await tool.execute?.(
      { question: "Which datasource?" },
      {
        agent: {
          toolCallId: "call-ask",
          suspend: async (payload: unknown) => {
            suspendedPayload = payload;
          }
        }
      }
    );

    expect(actionRouterCalled).toBe(false);
    expect(result).toBeUndefined();
    expect(suspendedPayload).toEqual({ question: "Which datasource?" });
  });

  it("warns the model not to repeat an external action whose state commit failed", async () => {
    const emitted: unknown[] = [];
    const runScope = { modelName: "test", resourceId: "user-1", runId: "run-1", sessionId: "session-1" };
    const boundary = createToolObservationBoundary({
      additionalAdapters: [{
        toolName: "side_effect_tool",
        resultType: "test",
        sourceType: "tool-observation",
        toContextItems: () => []
      }],
      identity: runScope
    });
    const dispatcher = new ToolObservationDispatcher(boundary.packager, runScope);
    const factory = new GovernedToolFactory(dispatcher, undefined, undefined, {
      actionRouter: {
        execute: async () => {
          throw toToolExecutionError(new Error("PROTOCOL_COMMIT_CONTENTION:complete_action"), {
            executionStatus: "succeeded_uncommitted",
            idempotency: "none",
            toolName: "side_effect_tool"
          }, { rawResult: { created: true } });
        }
      },
      emitter: { emit: (event) => emitted.push(event) },
      runId: "run-1",
      segmentId: "segment-1"
    });
    const rawTool: { execute(...args: unknown[]): Promise<unknown> } = {
      execute: async () => ({ shouldNotRun: true })
    };
    const tool = factory.governTool("side_effect_tool", rawTool);

    await expect(tool.execute?.({}, { agent: { toolCallId: "call-contention" } }))
      .rejects.toThrow("PROTOCOL_COMMIT_CONTENTION");

    const content = (emitted[0] as { content?: string } | undefined)?.content;
    expect(JSON.parse(content ?? "{}")).toMatchObject({
      ok: false,
      isError: true,
      error: {
        code: "PROTOCOL_COMMIT_CONTENTION",
        category: "concurrency",
        executionStatus: "succeeded_uncommitted",
        retryable: false
      },
      recovery: {
        strategy: "refresh_and_replan",
        avoid: ["Do not immediately repeat side_effect_tool; its external execution already completed."]
      }
    });
  });
});

const createProtocol = (): AgentProtocolDefinition<Record<string, never>> => ({
  id: "test/protocol",
  version: "1",
  initialPhase: "active",
  phases: { active: { allowedActions: [], transitions: [] } },
  createInitialState: () => ({}),
  completionPolicy: () => ({ status: "continue", reasons: ["not done"], allowedActions: [] })
});
