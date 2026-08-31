import { toToolExecutionError } from "../errors/tool-execution-error.js";
import { AGENT_RUNTIME_LIMITS } from "../config/agent-runtime-limits.js";
import type { ContextPackageRef, ProtocolGuardResult } from "../protocol/types.js";
import type { ProtocolRuntime } from "../protocol/protocol-runtime.js";
import type { CapabilityRegistry } from "./capability-registry.js";

export type ActionRouterOptions = {
  afterPreparatoryActions?(input: {
    actionName: string;
    domain: unknown;
    input: unknown;
    phase: string;
  }): Promise<void> | void;
  afterAction?(input: {
    actionName: string;
    rawResult: unknown;
  }): Promise<void> | void;
  preparatoryActions?(input: {
    actionName: string;
    input: unknown;
  }): Array<{ actionName: string; input: unknown }>;
  automaticActions?(input: {
    actionName: string;
    domain: unknown;
    input: unknown;
    observation: unknown;
    rawResult: unknown;
  }): Array<{ actionName: string; input: unknown }>;
  serverPolicy(input: { actionName: string; input: unknown }): ProtocolGuardResult;
  resourceAuthorization?(input: { actionName: string; input: unknown }): ProtocolGuardResult;
  projectContext(input: {
    actionName: string;
    observation: unknown;
    rawResult: unknown;
  }): ContextPackageRef | ActionContextProjection;
  projectProtocolEventResult?(input: {
    actionName: string;
    rawResult: unknown;
  }): unknown;
  projectFinalObservation?(input: {
    actionName: string;
    domain: unknown;
    observation: unknown;
    rawResult: unknown;
  }): unknown;
};

export type ActionContextProjection = {
  contextPackageRef: ContextPackageRef;
  contextPackage?: unknown;
  observation?: unknown;
};

export type ExecuteActionInput = {
  runId: string;
  segmentId: string;
  actionId: string;
  actionName: string;
  input: unknown;
  idempotencyKey?: string;
  abortSignal?: AbortSignal;
  invocationArgs?: unknown[];
  automaticDepth?: number;
};

export type ActionExecutionResult = {
  rawResult: unknown;
  observation: unknown;
  contextPackageRef: ContextPackageRef;
  contextPackage?: unknown;
};

export class ActionRouter<TDomainState> {
  private readonly idempotentResults = new Map<string, ActionExecutionResult>();

  constructor(
    private readonly registry: CapabilityRegistry,
    private protocolRuntime: ProtocolRuntime<TDomainState>,
    private readonly options: ActionRouterOptions
  ) {}

  async execute(input: ExecuteActionInput): Promise<ActionExecutionResult> {
    const policy = this.options.serverPolicy({ actionName: input.actionName, input: input.input });
    if (!policy.allowed) {
      this.rejectAction(input, policy.reasonCode, `ACTION_REJECTED:${policy.reasonCode}:${input.actionName}`);
    }
    const authorization = this.options.resourceAuthorization?.({
      actionName: input.actionName,
      input: input.input
    });
    if (authorization && !authorization.allowed) {
      this.rejectAction(
        input,
        authorization.reasonCode,
        `ACTION_REJECTED:${authorization.reasonCode}:${input.actionName}`
      );
    }
    const automaticDepth = input.automaticDepth ?? 0;
    const preparatoryActions = this.options.preparatoryActions?.({
      actionName: input.actionName,
      input: input.input
    }) ?? [];
    if (preparatoryActions.length > 0 && automaticDepth >= AGENT_RUNTIME_LIMITS.protocolAutomaticActionMaxDepth) {
      throw new Error("PROTOCOL_AUTOMATIC_ACTION_DEPTH_EXCEEDED");
    }
    for (const [index, preparatoryAction] of preparatoryActions.entries()) {
      await this.execute({
        runId: input.runId,
        segmentId: input.segmentId,
        actionId: `${input.actionId}:prepare:${index + 1}`,
        actionName: preparatoryAction.actionName,
        input: preparatoryAction.input,
        automaticDepth: automaticDepth + 1,
        idempotencyKey: `${input.actionId}:prepare:${index + 1}`,
        ...(input.abortSignal ? { abortSignal: input.abortSignal } : {})
      });
    }
    const preparedState = this.protocolRuntime.getState(input.runId, input.segmentId);
    await this.options.afterPreparatoryActions?.({
      actionName: input.actionName,
      domain: preparedState.domain,
      input: input.input,
      phase: preparedState.phase
    });
    const registered = this.registry.resolve(input.actionName);
    if (!registered) {
      throw new Error(`CAPABILITY_ACTION_NOT_REGISTERED:${input.actionName}`);
    }
    const parsedInput = registered.action.inputSchema.parse(input.input);
    const pluginGuard = await registered.action.guard?.(parsedInput);
    if (pluginGuard && !pluginGuard.allowed) {
      throw new Error(`ACTION_REJECTED:${pluginGuard.reasonCode}:${input.actionName}`);
    }
    if (registered.action.idempotency === "required" && !input.idempotencyKey) {
      throw new Error(`ACTION_IDEMPOTENCY_KEY_REQUIRED:${input.actionName}`);
    }
    const idempotencyKey = input.idempotencyKey && registered.action.idempotency !== "none"
      ? `${input.runId}:${input.segmentId}:${input.actionName}:${input.idempotencyKey}`
      : undefined;
    const existing = idempotencyKey ? this.idempotentResults.get(idempotencyKey) : undefined;
    if (existing) {
      return existing;
    }
    try {
      this.protocolRuntime.beginAction({
        runId: input.runId,
        segmentId: input.segmentId,
        actionId: input.actionId,
        actionName: input.actionName,
        actionInput: parsedInput
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : "ACTION_NOT_ALLOWED";
      if (isAdmissionRejection(message)) {
        this.rejectAction(input, message.split(":", 1)[0] ?? "ACTION_NOT_ALLOWED", message);
      }
      throw error;
    }
    let executionResult: unknown;
    let rawResult: unknown;
    let observation: unknown;
    let contextPackageRef: ContextPackageRef;
    let contextPackage: unknown;
    try {
      executionResult = await registered.action.execute({
        actionId: input.actionId,
        actionName: input.actionName,
        runId: input.runId,
        segmentId: input.segmentId,
        ...(input.abortSignal ? { abortSignal: input.abortSignal } : {}),
        ...(input.invocationArgs ? { invocationArgs: input.invocationArgs } : {})
      }, parsedInput);
    } catch (error) {
      try {
        this.protocolRuntime.recordActionFailure({
          runId: input.runId,
          segmentId: input.segmentId,
          actionId: input.actionId,
          actionName: input.actionName,
          reasonCode: error instanceof Error ? error.message : "ACTION_EXECUTION_FAILED"
        });
      } catch {
        // Preserve the executor failure: state contention must not replace the actionable root cause.
      }
      throw toToolExecutionError(error, {
        executionStatus: "failed",
        idempotency: registered.action.idempotency,
        toolName: input.actionName
      });
    }
    try {
      rawResult = registered.action.outputSchema.parse(executionResult);
      const initialObservation = registered.action.projectObservation?.(rawResult) ?? rawResult;
      const projection = this.options.projectContext({
        actionName: input.actionName,
        observation: initialObservation,
        rawResult
      });
      const normalizedProjection = isContextPackageRef(projection)
        ? { contextPackageRef: projection }
        : projection;
      observation = normalizedProjection.observation ?? initialObservation;
      contextPackageRef = normalizedProjection.contextPackageRef;
      contextPackage = normalizedProjection.contextPackage;
      this.protocolRuntime.recordActionSuccess({
        runId: input.runId,
        segmentId: input.segmentId,
        actionId: input.actionId,
        actionName: input.actionName,
        ...(registered.action.reduce
          ? { reduceDomain: (domain: TDomainState) => registered.action.reduce?.(domain, rawResult) as TDomainState }
          : {}),
        ...(this.options.projectProtocolEventResult
          ? { eventResult: this.options.projectProtocolEventResult({ actionName: input.actionName, rawResult }) }
          : {}),
        outputContextPackageRef: contextPackageRef
      });
    } catch (error) {
      if (!isProtocolCommitContention(error)) {
        try {
          this.protocolRuntime.recordActionFailure({
            runId: input.runId,
            segmentId: input.segmentId,
            actionId: input.actionId,
            actionName: input.actionName,
            reasonCode: error instanceof Error ? error.message : "ACTION_RESULT_PROCESSING_FAILED"
          });
        } catch {
          // The model-facing error below still preserves the original post-execution failure.
        }
      }
      throw toToolExecutionError(error, {
        executionStatus: "succeeded_uncommitted",
        idempotency: registered.action.idempotency,
        toolName: input.actionName
      }, {
        rawResult: executionResult
      });
    }
    let result: ActionExecutionResult = {
      rawResult,
      observation,
      contextPackageRef,
      ...(contextPackage === undefined ? {} : { contextPackage })
    };
    await this.options.afterAction?.({ actionName: input.actionName, rawResult });
    const automaticActions = this.options.automaticActions?.({
      actionName: input.actionName,
      domain: this.protocolRuntime.getState(input.runId, input.segmentId).domain,
      input: input.input,
      observation,
      rawResult
    }) ?? [];
    if (automaticActions.length > 0 && automaticDepth >= 10) {
      throw new Error("PROTOCOL_AUTOMATIC_ACTION_DEPTH_EXCEEDED");
    }
    for (const [index, automaticAction] of automaticActions.entries()) {
      await this.execute({
        runId: input.runId,
        segmentId: input.segmentId,
        actionId: `${input.actionId}:auto:${index + 1}`,
        actionName: automaticAction.actionName,
        input: automaticAction.input,
        automaticDepth: automaticDepth + 1,
        idempotencyKey: `${input.actionId}:auto:${index + 1}`,
        ...(input.abortSignal ? { abortSignal: input.abortSignal } : {})
      });
    }
    if (this.options.projectFinalObservation) {
      result = {
        ...result,
        observation: this.options.projectFinalObservation({
          actionName: input.actionName,
          domain: this.protocolRuntime.getState(input.runId, input.segmentId).domain,
          observation: result.observation,
          rawResult: result.rawResult
        })
      };
    }
    if (idempotencyKey) {
      this.idempotentResults.set(idempotencyKey, result);
    }
    return result;
  }

  replaceProtocolRuntime(runtime: ProtocolRuntime<TDomainState>): void {
    this.protocolRuntime = runtime;
  }

  private rejectAction(input: ExecuteActionInput, reasonCode: string, message: string): never {
    this.protocolRuntime.recordActionRejection({
      runId: input.runId,
      segmentId: input.segmentId,
      actionId: input.actionId,
      actionName: input.actionName,
      reasonCode
    });
    throw new Error(message);
  }
}

const isContextPackageRef = (
  value: ContextPackageRef | ActionContextProjection
): value is ContextPackageRef => "packageId" in value && "revision" in value;

const isAdmissionRejection = (message: string): boolean =>
  message.startsWith("ACTION_NOT_ALLOWED_IN_PHASE:")
  || message.startsWith("PROTOCOL_ACTION_BUDGET_EXHAUSTED:")
  || message.startsWith("PROTOCOL_GUARD_REJECTED:");

const isProtocolCommitContention = (error: unknown): boolean =>
  error instanceof Error && error.message.startsWith("PROTOCOL_COMMIT_CONTENTION:");
