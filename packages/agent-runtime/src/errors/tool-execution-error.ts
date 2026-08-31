import { AGENT_RUNTIME_LIMITS } from "../config/agent-runtime-limits.js";

export type ToolErrorCategory =
  | "authorization"
  | "concurrency"
  | "execution"
  | "protocol"
  | "upstream"
  | "validation";

export type ToolExecutionStatus = "not_started" | "failed" | "succeeded_uncommitted";

export type ToolRecoveryStrategy =
  | "handoff"
  | "refresh_and_replan"
  | "refresh_and_retry"
  | "request_user_input"
  | "retry_after_delay"
  | "retry_same_action"
  | "stop"
  | "use_existing_result";

export type ToolErrorObservation = {
  ok: false;
  isError: true;
  error: {
    code: string;
    category: ToolErrorCategory;
    message: string;
    executionStatus: ToolExecutionStatus;
    retryable: boolean;
    details?: Record<string, unknown>;
  };
  recovery: {
    strategy: ToolRecoveryStrategy;
    instruction: string;
    avoid: string[];
  };
};

export type ToolExecutionErrorOptions = {
  cause?: unknown;
  rawResult?: unknown;
};

export type ToolErrorContext = {
  executionStatus?: ToolExecutionStatus;
  idempotency?: "required" | "supported" | "none";
  toolName: string;
};

/** Error carrying a stable, model-facing observation separately from internal diagnostics. */
export class ToolExecutionError extends Error {
  readonly cause: unknown;
  readonly observation: ToolErrorObservation;
  readonly rawResult: unknown;

  constructor(observation: ToolErrorObservation, options: ToolExecutionErrorOptions = {}) {
    super(`${observation.error.code}: ${observation.error.message}`);
    this.name = "ToolExecutionError";
    this.cause = options.cause;
    this.observation = observation;
    this.rawResult = options.rawResult;
  }
}

/** Convert any internal failure into a stable error contract that tells the model how to recover. */
export const toToolExecutionError = (
  error: unknown,
  context: ToolErrorContext,
  options: Omit<ToolExecutionErrorOptions, "cause"> = {}
): ToolExecutionError => {
  if (error instanceof ToolExecutionError) {
    return error;
  }
  return new ToolExecutionError(createToolErrorObservation(error, context), {
    cause: error,
    ...options
  });
};

/** Return the public observation for an error without exposing internal stack traces or credentials. */
export const toolErrorObservation = (error: unknown, context: ToolErrorContext): ToolErrorObservation =>
  error instanceof ToolExecutionError ? error.observation : createToolErrorObservation(error, context);

const createToolErrorObservation = (error: unknown, context: ToolErrorContext): ToolErrorObservation => {
  const rawMessage = errorMessage(error);
  const code = errorCode(rawMessage);
  const executionStatus = context.executionStatus ?? inferExecutionStatus(code);

  if (code === "ACTION_NOT_ALLOWED_IN_PHASE") {
    const [, phase = "unknown", actionName = context.toolName, allowedSegment = ""] = rawMessage.split(":");
    const allowedActions = allowedSegment.split(",").filter((name) => name.length > 0);
    return observation({
      code,
      category: "protocol",
      message: `Tool ${actionName} is not allowed in protocol phase ${phase}.`,
      executionStatus: "not_started",
      retryable: false,
      ...(allowedActions.length > 0 ? { details: { allowedActions } } : {}),
      strategy: "refresh_and_replan",
      instruction: allowedActions.length > 0
        ? `Continue with one of the actions allowed in phase ${phase}: ${formatActionList(allowedActions)}.`
        : "Choose an action allowed in the current phase before calling this tool again.",
      avoid: [`Do not repeat ${actionName} while the protocol remains in phase ${phase}.`]
    });
  }

  if (code === "PROTOCOL_COMMIT_CONTENTION" || code === "PROTOCOL_REVISION_CONFLICT") {
    const canRetry = context.idempotency !== "none" && executionStatus !== "not_started";
    const alreadyExecuted = executionStatus === "succeeded_uncommitted";
    return observation({
      code: "PROTOCOL_COMMIT_CONTENTION",
      category: "concurrency",
      message: alreadyExecuted
        ? `Tool ${context.toolName} completed, but its protocol state could not be committed after concurrent updates.`
        : `Tool ${context.toolName} could not update protocol state because concurrent updates did not settle.`,
      executionStatus,
      retryable: canRetry,
      strategy: canRetry ? "refresh_and_retry" : "refresh_and_replan",
      instruction: canRetry
        ? "Refresh the protocol state, then retry only if the action is still needed and allowed."
        : "Refresh the protocol state and replan without assuming that repeating this action is safe.",
      avoid: [alreadyExecuted
        ? `Do not immediately repeat ${context.toolName}; its external execution already completed.`
        : `Do not retry ${context.toolName} against the stale protocol state.`]
    });
  }

  if (code === "ACTION_REJECTED" || code === "PROTOCOL_GUARD_REJECTED") {
    return observation({
      code,
      category: code === "ACTION_REJECTED" ? "authorization" : "validation",
      message: safeReason(rawMessage, `Tool ${context.toolName} was rejected before execution.`),
      executionStatus: "not_started",
      retryable: false,
      strategy: "refresh_and_replan",
      instruction: "Correct the rejected precondition or choose another allowed action.",
      avoid: [`Do not repeat ${context.toolName} with unchanged input and state.`]
    });
  }

  if (isSchemaValidationError(error)) {
    return observation({
      code: executionStatus === "succeeded_uncommitted" ? "ACTION_RESULT_INVALID" : "ACTION_INPUT_INVALID",
      category: "validation",
      message: safeReason(rawMessage, `Tool ${context.toolName} returned data that did not match its contract.`),
      executionStatus,
      retryable: false,
      strategy: "refresh_and_replan",
      instruction: "Use a corrected input or an alternative tool whose contract matches the required data.",
      avoid: [`Do not repeat ${context.toolName} unchanged after the same contract validation failure.`]
    });
  }

  if (isUpstreamFailure(code, rawMessage)) {
    return observation({
      code,
      category: "upstream",
      message: safeReason(rawMessage, `The upstream service used by ${context.toolName} failed.`),
      executionStatus,
      retryable: true,
      strategy: "retry_after_delay",
      instruction: "Wait briefly, then retry once; use an alternative source if the upstream failure continues.",
      avoid: [`Do not loop on ${context.toolName} without delay or a retry limit.`]
    });
  }

  return observation({
    code,
    category: code.includes("INPUT")
      || code.includes("SCHEMA")
      || code.includes("NOT_FOUND")
      || code.includes("UNKNOWN")
      ? "validation"
      : "execution",
    message: safeReason(rawMessage, `Tool ${context.toolName} failed.`),
    executionStatus,
    retryable: false,
    strategy: "refresh_and_replan",
    instruction: "Use the failure reason to correct the input or choose a different action.",
    avoid: [`Do not repeat ${context.toolName} unchanged after the same failure.`]
  });
};

const observation = (input: {
  code: string;
  category: ToolErrorCategory;
  message: string;
  executionStatus: ToolExecutionStatus;
  retryable: boolean;
  details?: Record<string, unknown>;
  strategy: ToolRecoveryStrategy;
  instruction: string;
  avoid: string[];
}): ToolErrorObservation => ({
  ok: false,
  isError: true,
  error: {
    code: input.code,
    category: input.category,
    message: input.message,
    executionStatus: input.executionStatus,
    retryable: input.retryable,
    ...(input.details ? { details: input.details } : {})
  },
  recovery: {
    strategy: input.strategy,
    instruction: input.instruction,
    avoid: input.avoid
  }
});

const formatActionList = (actions: string[]): string =>
  actions.length <= 12
    ? actions.join(", ")
    : `${actions.slice(0, 12).join(", ")} (+${actions.length - 12} more)`;

const errorMessage = (error: unknown): string => {
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === "string") {
    return error;
  }
  return "ACTION_EXECUTION_FAILED";
};

const errorCode = (message: string): string => {
  const candidate = message.split(":", 1)[0]?.trim();
  return candidate && /^[A-Z][A-Z0-9_]+$/.test(candidate) ? candidate : "ACTION_EXECUTION_FAILED";
};

const inferExecutionStatus = (code: string): ToolExecutionStatus =>
  code === "ACTION_NOT_ALLOWED_IN_PHASE"
    || code === "ACTION_REJECTED"
    || code === "PROTOCOL_GUARD_REJECTED"
    || code === "CAPABILITY_ACTION_NOT_REGISTERED"
    ? "not_started"
    : "failed";

const isUpstreamFailure = (code: string, message: string): boolean =>
  code.includes("TIMEOUT")
  || code.includes("NETWORK")
  || code.startsWith("MCP_")
  || /\b(fetch|socket|connection|upstream|ECONN|ETIMEDOUT)\b/i.test(message);

const isSchemaValidationError = (error: unknown): boolean =>
  error instanceof Error && (error.name === "ZodError" || error.name === "ValidationError");

const safeReason = (message: string, fallback: string): string => {
  const compact = message.replace(/\s+/g, " ").trim();
  if (!compact) {
    return fallback;
  }
  return compact
    .replace(/(api[_-]?key|authorization|token|password)\s*[=:]\s*[^\s,;]+/gi, "$1=[REDACTED]")
    .slice(0, AGENT_RUNTIME_LIMITS.toolErrorMaxMessageChars);
};
