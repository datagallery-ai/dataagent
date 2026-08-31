/**
 * Single, ordered assembly pipeline for a run's tool set. Every tool that enters or
 * leaves the exposed set does so with a recorded reason, so "why does the model
 * (not) see this tool" is answerable from the plan instead of from a debugger.
 *
 * Stages, in order:
 *   1. groups        — resource-gated tool groups merge in declaration order
 *                      (later groups override earlier names, matching spread order).
 *   2. skill policy  — deny list, then allow list with always-allow and skill-meta
 *                      exemptions (same semantics the run previously applied inline).
 *   3. mcpTools      — merged after the skill policy BY DESIGN: MCP tools are
 *                      governed by their own per-server allowlist (policy-mcp
 *                      middleware), not by skill allow/deny sets.
 *
 * The assembled schema is static, while resolveToolPlanAvailability projects the
 * active protocol/phase onto it. The protocol runtime action gate remains authoritative.
 */

import { isDataActionName } from "../protocol/data-actions.js";

export type ToolPlanEntry = {
  name: string;
  source: string;
  exposed: boolean;
  availability: "available" | "protocol-disabled";
  recovery?: { action: "protocol-handoff"; targetProtocolId: string };
  reasons: string[];
};

export type ToolPlan<TTool> = {
  entries: ToolPlanEntry[];
  exposedTools: Record<string, TTool>;
};

export type SkillToolPolicy = {
  allowedTools?: string[] | undefined;
  deniedTools: string[];
};

const SKILL_META_TOOLS = new Set(["skill", "skill_search", "skill_read"]);

export const buildToolPlan = <TTool>(input: {
  groups: Array<{ source: string; tools: Record<string, TTool> }>;
  mcpTools?: Record<string, TTool> | undefined;
  alwaysAllow?: ReadonlySet<string>;
  skillPolicy?: SkillToolPolicy | undefined;
}): ToolPlan<TTool> => {
  const denied = new Set(input.skillPolicy?.deniedTools ?? []);
  const allowed = input.skillPolicy?.allowedTools ? new Set(input.skillPolicy.allowedTools) : undefined;
  const alwaysAllow = input.alwaysAllow ?? new Set<string>();
  const merged = new Map<string, { source: string; tool: TTool }>();
  for (const group of input.groups) {
    for (const [name, tool] of Object.entries(group.tools)) {
      merged.set(name, { source: group.source, tool });
    }
  }
  const entries: ToolPlanEntry[] = [];
  const exposedTools: Record<string, TTool> = {};
  for (const [name, { source, tool }] of merged) {
    const reasons = [`source:${source}`];
    let exposed = true;
    if (denied.has(name)) {
      exposed = false;
      reasons.push("skill-policy:denied");
    } else if (alwaysAllow.has(name)) {
      reasons.push("always-allow");
    } else if (!allowed) {
      reasons.push("skill-policy:open");
    } else if (allowed.has(name)) {
      reasons.push("skill-policy:allowed");
    } else if (SKILL_META_TOOLS.has(name)) {
      reasons.push("skill-meta");
    } else {
      exposed = false;
      reasons.push("skill-policy:not-allowed");
    }
    entries.push({ name, source, exposed, availability: "available", reasons });
    if (exposed) {
      exposedTools[name] = tool;
    }
  }
  for (const [name, tool] of Object.entries(input.mcpTools ?? {})) {
    entries.push({
      name,
      source: "mcp",
      exposed: true,
      availability: "available",
      reasons: ["source:mcp", "mcp-policy:server-allowlist"]
    });
    exposedTools[name] = tool;
  }
  return { entries, exposedTools };
};

const TOOL_ACTION_ALIASES: Record<string, string> = {
  protocol_handoff: "protocol.handoff.propose",
  analysis_requirements_commit: "analysis.requirements.commit"
};

/** Resolve the protocol/phase availability of the static agent tool schema. */
export const resolveToolPlanAvailability = (input: {
  entries: ToolPlanEntry[];
  protocolId: string;
  phase: string;
  allowedActions: string[];
}): ToolPlanEntry[] => {
  const allowed = new Set(input.allowedActions);
  return input.entries.map((entry) => {
    if (!entry.exposed) return { ...entry };
    const actionName = TOOL_ACTION_ALIASES[entry.name] ?? entry.name;
    const available = allowed.has(actionName);
    return {
      ...entry,
      availability: available ? "available" : "protocol-disabled",
      reasons: [
        ...entry.reasons.filter((reason) => !reason.startsWith("protocol:")),
        `protocol:${input.protocolId}:${input.phase}:${available ? "allowed" : "disabled"}`
      ],
      ...(!available && input.protocolId === "general-task"
        && (isDataActionName(actionName) || actionName === "analysis.requirements.commit")
        ? { recovery: { action: "protocol-handoff" as const, targetProtocolId: "data-analysis" } }
        : {})
    };
  });
};
