import { EventType, type BaseEvent } from "@ag-ui/client";
import type { RunEventWriter } from "@datafoundry/metadata";

import type { ConversationMemoryEventObserver } from "./conversation-memory.js";
import type { RunCheckpointProjector } from "./run-checkpoint-projector.js";
import type { TaskPlanProjector } from "./task-plan-projector.js";
import type { TraceSectionCoordinator } from "./trace-section-coordinator.js";
import type { ToolCallResultBridge } from "./tool-call-result-bridge.js";

type RunEventPipelineInput = {
  checkpointProjector?: RunCheckpointProjector;
  conversationMemoryObserver: ConversationMemoryEventObserver;
  runEventWriter: RunEventWriter;
  runId: string;
  sessionId: string;
  taskPlanProjector: TaskPlanProjector;
  traceSectionCoordinator?: TraceSectionCoordinator;
  toolCallResultBridge: ToolCallResultBridge;
  userId: string;
  sink(event: BaseEvent): void;
};

/** Persist, project, and deliver AG-UI run events in one ordered pipeline. */
export class RunEventPipeline {
  private readonly input: RunEventPipelineInput;
  private projecting = false;
  /** toolCallIds that already delivered a TOOL_CALL_RESULT — later duplicates are dropped. */
  private readonly deliveredToolResults = new Set<string>();

  constructor(input: RunEventPipelineInput) {
    this.input = input;
  }

  emit(event: BaseEvent): void {
    this.emitObserved(event, true);
  }

  private emitObserved(event: BaseEvent, allowProjection: boolean): void {
    if (isTerminalEvent(event)) {
      this.input.toolCallResultBridge.flushPendingResults().forEach((payload) => this.deliver(payload));
    }

    this.deliver(event);
    this.input.toolCallResultBridge.observe(event).forEach((payload) => this.deliver(payload));

    if (!allowProjection || this.projecting) {
      return;
    }

    const projectedEvents = this.input.taskPlanProjector.observe(event);
    if (projectedEvents.length === 0) {
      return;
    }

    this.projecting = true;
    try {
      projectedEvents.forEach((projectedEvent) => this.emitObserved(projectedEvent, false));
    } finally {
      this.projecting = false;
    }
  }

  private deliver(event: BaseEvent): void {
    if (event.type === EventType.TOOL_CALL_RESULT) {
      const toolCallId = typeof event.toolCallId === "string" ? event.toolCallId : undefined;
      if (toolCallId) {
        if (this.deliveredToolResults.has(toolCallId)) {
          return;
        }
        this.deliveredToolResults.add(toolCallId);
      }
    }
    const envelope = this.input.runEventWriter.write({
      user_id: this.input.userId,
      run_id: this.input.runId,
      session_id: this.input.sessionId,
      event
    });
    this.input.checkpointProjector?.observe(envelope);
    this.input.traceSectionCoordinator?.observe(envelope);
    this.input.conversationMemoryObserver.observe(event);
    this.input.sink(event);
  }
}

const isTerminalEvent = (event: BaseEvent): boolean =>
  event.type === EventType.RUN_FINISHED || event.type === EventType.RUN_ERROR;
