import { EventType } from "@ag-ui/client";
import type { RunEventEnvelope } from "@datafoundry/contracts";
import type {
  CheckpointKind,
  CheckpointStatus,
  ContextPackageSnapshotRecord,
  MetadataStore
} from "@datafoundry/metadata";

export class RunCheckpointProjector {
  constructor(
    private readonly metadataStore: MetadataStore,
    private readonly userId: string
  ) {}

  observe(envelope: RunEventEnvelope): void {
    const event = envelope.event as Record<string, unknown>;
    if (event.type === EventType.CUSTOM && event.name === "context.compiled") {
      this.createContextCompiledCheckpoint(envelope, event.value);
      return;
    }

    if (event.type === EventType.CUSTOM && event.name === "protocol.phase.entered") {
      const value = recordValue(event.value);
      const payload = recordValue(value?.payload);
      this.createLatestCheckpoint(envelope, {
        kind: "protocol-phase",
        label: `Protocol phase: ${stringValue(payload?.phase) ?? "unknown"}`,
        status: "stable"
      });
      return;
    }

    if (event.type === EventType.TOOL_CALL_RESULT) {
      this.createLatestCheckpoint(envelope, {
        kind: "tool-result",
        label: toolResultLabel(event),
        status: "stable",
        toolCallId: stringValue(event.toolCallId) ?? stringValue(event.tool_call_id)
      });
      return;
    }

    if (event.type === EventType.RUN_FINISHED || event.type === EventType.RUN_ERROR) {
      this.createLatestCheckpoint(envelope, {
        kind: "run-terminal",
        label: event.type === EventType.RUN_ERROR ? "Run failed" : "Run completed",
        status: event.type === EventType.RUN_ERROR ? "failed" : "terminal"
      });
    }
  }

  private createContextCompiledCheckpoint(envelope: RunEventEnvelope, value: unknown): void {
    const payload = recordValue(value);
    const packageId = stringValue(payload?.package_id);
    const revision = numberValue(payload?.package_revision);
    if (!packageId || revision === undefined) {
      return;
    }
    const snapshot = this.metadataStore.contextPackageSnapshots.findByPackageRevision({
      user_id: this.userId,
      package_id: packageId,
      revision
    });
    if (!snapshot) {
      return;
    }
    this.createCheckpoint(envelope, snapshot, {
      kind: "context-compiled",
      label: `Context step ${numberValue(payload?.step_number) ?? revision}`,
      status: "stable",
      contextPlanId: stringValue(payload?.plan_id),
      stepNumber: numberValue(payload?.step_number)
    });
  }

  private createLatestCheckpoint(
    envelope: RunEventEnvelope,
    input: {
      kind: CheckpointKind;
      label: string;
      status: CheckpointStatus;
      toolCallId?: string | undefined;
    }
  ): void {
    const snapshot = this.metadataStore.contextPackageSnapshots.latestByRun({
      user_id: this.userId,
      run_id: envelope.run_id
    });
    if (!snapshot) {
      return;
    }
    this.createCheckpoint(envelope, snapshot, input);
  }

  private createCheckpoint(
    envelope: RunEventEnvelope,
    snapshot: ContextPackageSnapshotRecord,
    input: {
      contextPlanId?: string | undefined;
      kind: CheckpointKind;
      label: string;
      status: CheckpointStatus;
      stepNumber?: number | undefined;
      toolCallId?: string | undefined;
    }
  ): void {
    const parent = this.metadataStore.checkpoints.latestByRun({
      user_id: this.userId,
      run_id: envelope.run_id
    });
    const intentBinding = this.metadataStore.sessionIntents.findRunBinding({
      user_id: this.userId,
      run_id: envelope.run_id
    });
    this.metadataStore.checkpoints.create({
      id: `ckpt_${envelope.run_id}_${envelope.seq}_${input.kind}`,
      user_id: this.userId,
      session_id: envelope.session_id,
      run_id: envelope.run_id,
      event_seq: envelope.seq,
      context_package_id: snapshot.id,
      context_package_revision: snapshot.revision,
      kind: input.kind,
      status: input.status,
      label: input.label,
      ...(input.contextPlanId ? { context_plan_id: input.contextPlanId } : {}),
      ...(parent ? { parent_checkpoint_id: parent.id } : {}),
      ...(input.stepNumber !== undefined ? { step_number: input.stepNumber } : {}),
      ...(input.toolCallId ? { tool_call_id: input.toolCallId } : {}),
      ...(intentBinding?.active_revision_id
        ? { intent_revision_id: intentBinding.active_revision_id }
        : {})
    });
  }
}

const toolResultLabel = (event: { toolCallName?: unknown; toolName?: unknown; tool_name?: unknown }): string => {
  const name = stringValue(event.toolCallName) ?? stringValue(event.toolName) ?? stringValue(event.tool_name);
  return name ? `Tool result: ${name}` : "Tool result";
};

const recordValue = (value: unknown): Record<string, unknown> | undefined =>
  typeof value === "object" && value !== null ? value as Record<string, unknown> : undefined;

const stringValue = (value: unknown): string | undefined => typeof value === "string" ? value : undefined;

const numberValue = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;
