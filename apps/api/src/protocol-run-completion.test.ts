import { EventType, type BaseEvent } from "@ag-ui/client";
import type { ProtocolRunState } from "@datafoundry/agent-runtime";
import { describe, expect, it, vi } from "vitest";

import { assistantMessageIdFromEvent, completeProtocolRun } from "./protocol-run-completion.js";

const terminalEvent = { type: EventType.RUN_FINISHED, timestamp: 1 } as BaseEvent;

describe("completeProtocolRun", () => {
  it("recognizes a tool-only assistant turn through its parent message id", () => {
    expect(assistantMessageIdFromEvent({
      type: EventType.TOOL_CALL_START,
      toolCallId: "tool-1",
      toolCallName: "write_file",
      parentMessageId: "tool-parent-message"
    } as BaseEvent)).toBe("tool-parent-message");
  });

  it("uses the latest persisted assistant message when the current segment has no text event", async () => {
    const harness = createHarness({});

    await completeProtocolRun({
      ...harness.input,
      persistedAssistantMessageId: "persisted-message",
      terminalEvent
    });

    expect(harness.execute).toHaveBeenCalledWith(expect.objectContaining({
      actionName: "general.answer.commit",
      input: { messageId: "persisted-message" }
    }));
    expect(harness.complete).toHaveBeenCalledOnce();
  });

  it("does not submit a second answer after the protocol has entered the answer phase", async () => {
    const harness = createHarness({ answerMessageId: "committed-message", phase: "answer" });

    await completeProtocolRun({
      ...harness.input,
      lastAssistantMessageId: "committed-message",
      terminalEvent
    });

    expect(harness.execute).not.toHaveBeenCalled();
    expect(harness.complete).toHaveBeenCalledOnce();
  });

  it("finalizes as failed when general-task rejected the run's data actions", async () => {
    const harness = createHarness({
      actions: [
        rejectedAction("a1", "inspect_schema"),
        rejectedAction("a2", "inspect_schema"),
        rejectedAction("a3", "list_data_sources")
      ]
    });

    await completeProtocolRun({
      ...harness.input,
      lastAssistantMessageId: "apology-message",
      terminalEvent
    });

    expect(harness.execute).not.toHaveBeenCalled();
    expect(harness.complete).not.toHaveBeenCalled();
    expect(harness.input.protocol.protocolRuntime.proposeCompletion).not.toHaveBeenCalled();
    expect(harness.input.protocol.protocolRuntime.terminateFailure).toHaveBeenCalledWith(
      expect.objectContaining({ reasons: [expect.stringContaining("DATA_ACTIONS_REJECTED_BY_PROTOCOL")] })
    );
    expect(harness.fail).toHaveBeenCalledWith(expect.objectContaining({
      errorMessage: expect.stringContaining("DATA_ACTIONS_REJECTED_BY_PROTOCOL"),
      terminalEvent: expect.objectContaining({ type: EventType.RUN_ERROR })
    }));
    expect(harness.fail.mock.calls[0]?.[0]?.errorMessage).toContain("inspect_schema, list_data_sources");
  });

  it("keeps completing general-task runs whose rejected actions are not data actions", async () => {
    const harness = createHarness({
      actions: [rejectedAction("a1", "retrieve_knowledge")]
    });

    await completeProtocolRun({
      ...harness.input,
      lastAssistantMessageId: "message-1",
      terminalEvent
    });

    expect(harness.fail).not.toHaveBeenCalled();
    expect(harness.complete).toHaveBeenCalledOnce();
  });

  it("does not gate data-analysis runs on phase-rejected data actions", async () => {
    const harness = createHarness({
      protocolId: "data-analysis",
      answerMessageId: "n/a",
      actions: [rejectedAction("a1", "run_sql_readonly")]
    });

    await completeProtocolRun({
      ...harness.input,
      lastAssistantMessageId: "message-1",
      terminalEvent
    });

    expect(harness.fail).not.toHaveBeenCalled();
    expect(harness.complete).toHaveBeenCalledOnce();
  });

  it("emits a clean run error when terminal protocol finalization fails", async () => {
    const harness = createHarness({});
    harness.execute.mockRejectedValueOnce(new Error("ACTION_NOT_ALLOWED_IN_PHASE:answer:general.answer.commit"));

    await expect(completeProtocolRun({
      ...harness.input,
      lastAssistantMessageId: "message-1",
      terminalEvent
    })).resolves.toBeUndefined();

    expect(harness.fail).toHaveBeenCalledWith({
      errorMessage: "ACTION_NOT_ALLOWED_IN_PHASE:answer:general.answer.commit",
      terminalEvent: expect.objectContaining({
        type: EventType.RUN_ERROR,
        message: "ACTION_NOT_ALLOWED_IN_PHASE:answer:general.answer.commit"
      })
    });
  });
});

const rejectedAction = (actionId: string, actionName: string): ProtocolRunState["actions"][number] => ({
  actionId,
  actionName,
  status: "rejected",
  inputContextPackageRef: { packageId: "context-1", revision: 1 },
  reasonCode: "ACTION_NOT_ALLOWED_IN_PHASE"
});

const createHarness = (input: {
  answerMessageId?: string;
  phase?: string;
  protocolId?: string;
  actions?: ProtocolRunState["actions"];
}) => {
  const execute = vi.fn(async () => undefined);
  const complete = vi.fn(async () => undefined);
  const fail = vi.fn();
  let state: ProtocolRunState = {
    protocolId: input.protocolId ?? "general-task",
    protocolVersion: "1",
    runId: "run-1",
    segmentId: "segment-1",
    phase: input.phase ?? "gather",
    revision: 1,
    status: "active",
    contextPackageRef: { packageId: "context-1", revision: 1 },
    actions: input.actions ?? [],
    completionRejections: 0,
    domain: input.answerMessageId ? { answerMessageId: input.answerMessageId } : {},
  };
  const protocolRuntime = {
    getState: vi.fn(() => state),
    terminateFailure: vi.fn((failure: { reasons: string[] }) => {
      state = {
        ...state,
        revision: state.revision + 1,
        status: "terminal",
        terminalDecision: { status: "failed", reasons: failure.reasons }
      };
      return state;
    }),
    proposeCompletion: vi.fn(() => {
      state = {
        ...state,
        revision: state.revision + 1,
        terminalDecision: {
          status: "completed",
          evaluatedContextPackageRef: { packageId: "context-1", revision: 1 },
          evidenceRefs: []
        }
      };
      return state;
    })
  };
  execute.mockImplementation(async () => {
    state = { ...state, phase: "answer", domain: { answerMessageId: "persisted-message" } };
    return undefined;
  });
  return {
    complete,
    execute,
    fail,
    input: {
      finalizer: { complete, fail },
      protocol: { actionRouter: { execute }, protocolRuntime, segmentId: "segment-1" },
      runId: "run-1"
    }
  };
};
