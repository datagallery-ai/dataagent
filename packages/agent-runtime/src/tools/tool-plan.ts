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
 * Protocol-phase permissions are deliberately NOT part of the plan: they are
 * dynamic per phase and stay with the protocol runtime's action gate.
 */

export type ToolPlanEntry = {
  name: string;
  source: string;
  exposed: boolean;
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
    entries.push({ name, source, exposed, reasons });
    if (exposed) {
      exposedTools[name] = tool;
    }
  }
  for (const [name, tool] of Object.entries(input.mcpTools ?? {})) {
    entries.push({ name, source: "mcp", exposed: true, reasons: ["source:mcp", "mcp-policy:server-allowlist"] });
    exposedTools[name] = tool;
  }
  return { entries, exposedTools };
};
