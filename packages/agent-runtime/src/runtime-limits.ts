import { AGENT_RUNTIME_LIMITS } from "./config/agent-runtime-limits.js";

// Backward-compatible named exports. Definitions and environment overrides live in the central registry.
export const AGENT_MAX_STEPS = AGENT_RUNTIME_LIMITS.agentMaxSteps;

export const SQL_MAX_EXECUTION_COUNT = AGENT_RUNTIME_LIMITS.sqlMaxExecutionCount;
