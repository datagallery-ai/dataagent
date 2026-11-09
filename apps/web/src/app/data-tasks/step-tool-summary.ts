import { toolDisplayTitle } from "./data-task-state";
import type { LiveRun } from "./live-run-state";
import type { TranslateFn } from "../../i18n/types";

export type StepToolStatus = "running" | "success" | "failed";

type AssistantToolCallLike = {
  id?: string;
  function?: { name?: string; arguments?: string };
};

function toolSummaryStatus(callStatus?: LiveRun["toolCalls"][number]["status"]): StepToolStatus {
  if (callStatus === "failed") return "failed";
  if (callStatus === "running") return "running";
  return "success";
}

export function buildStepToolSummaries(input: {
  toolCalls: AssistantToolCallLike[];
  liveRun: LiveRun | null;
  isActive: boolean;
  t?: TranslateFn;
}): StepToolSummaryInput[] {
  const liveById = new Map(input.liveRun?.toolCalls.map((call) => [call.id, call]) ?? []);
  return input.toolCalls
    .map((call, index) => {
      const id = typeof call.id === "string" && call.id ? call.id : `tool-${index}`;
      const liveCall = liveById.get(id);
      const status = liveCall
        ? toolSummaryStatus(liveCall.status)
        : input.isActive
          ? "running"
          : "success";
      const name = liveCall?.name ?? call.function?.name;
      const title = toolDisplayTitle(name, input.t);
      const target = toolCallTarget(name, call.function?.arguments, liveCall?.progress?.agentName);
      return {
        id,
        label: target ? `${title} · ${target}` : title,
        status,
        durationLabel: liveCall
          ? stepElapsedLabel(liveCall, input.t)
          : status === "running"
            ? input.t
              ? input.t("common.running")
              : "Running"
            : "—",
        ...(liveCall?.startedAtMs !== undefined ? { startedAtMs: liveCall.startedAtMs } : {}),
      };
    })
    .filter((tool) => tool.label.trim().length > 0);
}

export type StepToolSummaryInput = {
  id: string;
  label: string;
  status: StepToolStatus;
  durationLabel: string;
  startedAtMs?: number;
};

const TOOL_TARGET_FIELDS = ["subagent_type", "file_path", "path", "command", "pattern", "sql", "query"] as const;

function toolCallTarget(name: string | undefined, rawArguments: string | undefined, agentName?: string): string | undefined {
  if (name === "task" && agentName) return clipTarget(agentName);
  const args = parseArguments(rawArguments);
  if (!args) return undefined;
  if (name === "task") return clipTarget(stringField(args, "subagent_type"));
  for (const field of TOOL_TARGET_FIELDS) {
    if (field === "subagent_type") continue;
    const value = stringField(args, field);
    if (value) return clipTarget(value);
  }
  return undefined;
}

function parseArguments(raw: string | undefined): Record<string, unknown> | undefined {
  if (!raw || raw === "{}") return undefined;
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return undefined;
    return parsed as Record<string, unknown>;
  } catch {
    return undefined;
  }
}

function stringField(record: Record<string, unknown>, field: string): string | undefined {
  const value = record[field];
  return typeof value === "string" && value.trim() ? value : undefined;
}

function clipTarget(value: string | undefined): string | undefined {
  if (!value) return undefined;
  const text = value.replace(/\s+/gu, " ").trim();
  if (!text) return undefined;
  return text.length > 48 ? `${text.slice(0, 48).trimEnd()}…` : text;
}

export type ToolChipSummary = StepToolSummaryInput & {
  overflow?: boolean;
};

export type StepElapsedInput = {
  status: StepToolStatus;
  startedAtMs?: number;
  finishedAtMs?: number;
};

export function runningElapsedLabel(startedAtMs: number, now = Date.now()): string {
  return `${Math.max(0, Math.floor((now - startedAtMs) / 1000))}s`;
}

export function formatStepDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function stepElapsedLabel(step: StepElapsedInput, t?: TranslateFn): string {
  if (step.status === "running") return t ? t("common.running") : "Running";
  if (step.startedAtMs === undefined || step.finishedAtMs === undefined) {
    return "—";
  }
  return formatStepDuration(Math.max(0, step.finishedAtMs - step.startedAtMs));
}

export function truncateThinkingPreview(text: string, maxChars = 96): string | undefined {
  const parts = text
    .split(/\s*\n+\s*/)
    .map((part) => part.trim())
    .filter(Boolean);
  const compact = parts
    .slice(0, 2)
    .join(" ")
    .trim();
  if (!compact) return undefined;
  if (compact.length > maxChars) return `${compact.slice(0, maxChars).trimEnd()}…`;
  return parts.length > 2 ? `${compact}…` : compact;
}

export function buildToolChipSummaries(
  tools: StepToolSummaryInput[],
  maxVisible = Number.POSITIVE_INFINITY,
): ToolChipSummary[] {
  if (tools.length <= maxVisible) return tools;
  const visibleCount = Math.max(1, maxVisible - 1);
  return [
    ...tools.slice(0, visibleCount),
    {
      id: "__overflow__",
      label: `+${tools.length - visibleCount}`,
      status: "success",
      durationLabel: "",
      overflow: true,
    },
  ];
}

function formatToolSummary(
  tools: StepToolSummaryInput[],
  options: { includeConcurrencyPrefix?: boolean } = {},
): string {
  if (tools.length === 0) return "No tool calls";
  const prefix =
    options.includeConcurrencyPrefix && tools.length > 1
      ? `${tools.length} tools in parallel · `
      : "";
  return `${prefix}${tools
    .map((tool) => [tool.label, tool.durationLabel].filter(Boolean).join(" "))
    .join(" · ")}`;
}

export function buildCollapsedStepSummary(input: {
  thinking?: string;
  tools: StepToolSummaryInput[];
}): {
  thinkingPreview?: string;
  toolSummary: string;
} {
  const thinkingPreview = truncateThinkingPreview(input.thinking ?? "");
  return {
    thinkingPreview,
    toolSummary: formatToolSummary(input.tools, {
      includeConcurrencyPrefix: !thinkingPreview,
    }),
  };
}
