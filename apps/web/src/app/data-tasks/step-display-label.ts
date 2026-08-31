const toolActionLabels: Record<string, string> = {
  list_data_sources: "List data sources",
  inspect_schema: "Inspect schema",
  preview_table: "Preview data",
  run_sql_readonly: "Run query",
  retrieve_knowledge: "Retrieve knowledge",
  read_file: "Read file",
  edit_file: "Edit file",
  write_file: "Write file",
  list_files: "Browse files",
  grep: "Search files",
  mkdir: "Create directory",
  file_stat: "Get file info",
  execute_command: "Run command",
  promote_workspace_file: "Promote workspace file",
  task_write: "Write task",
  task_update: "Update task",
  task_complete: "Complete task",
  task_check: "Check task",
  ask_user: "Waiting for confirmation",
  submit_plan: "Submit plan",
};

const SUMMARY_PREVIEW_MAX = 48;

const fileToolNames = new Set(["read_file", "edit_file", "write_file"]);
const dataToolNames = new Set([
  "list_data_sources",
  "inspect_schema",
  "preview_table",
  "run_sql_readonly",
]);
const workspaceToolNames = new Set([
  "list_files",
  "grep",
  "mkdir",
  "file_stat",
  "execute_command",
  "promote_workspace_file",
  "task_write",
  "task_update",
  "task_complete",
  "task_check",
]);

/** Strip MCP namespace prefix so `mcp__server__inspect_schema` resolves like `inspect_schema`. */
export function normalizeToolLookupName(toolName: string): string {
  const trimmed = toolName.trim();
  if (!trimmed.startsWith("mcp__")) {
    return trimmed;
  }
  const parts = trimmed.split("__");
  if (parts.length >= 3) {
    return parts.slice(2).join("__");
  }
  return trimmed;
}

export function isDisplayableToolName(toolName: string): boolean {
  const trimmed = toolName.trim();
  if (!trimmed) return false;
  const normalized = normalizeToolLookupName(trimmed).toLowerCase();
  return normalized !== "tool" && normalized !== "unknown";
}

function formatSnakeCaseLabel(toolName: string): string {
  return toolName
    .split("_")
    .filter(Boolean)
    .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
    .join(" ");
}

function resolveSingleToolStepActionLabel(toolName: string): string {
  const trimmed = toolName.trim();
  if (!trimmed) return "Thinking";

  const direct = toolActionLabels[trimmed];
  if (direct) return direct;

  const lookupName = normalizeToolLookupName(trimmed);
  const lookupLabel = toolActionLabels[lookupName];
  if (lookupLabel) return lookupLabel;

  if (/[\u4e00-\u9fff]/.test(trimmed)) {
    return trimmed;
  }

  if (lookupName !== trimmed) {
    const namespacedDisplay = toolActionLabels[lookupName];
    if (namespacedDisplay) return namespacedDisplay;
  }

  if (trimmed.includes("_")) {
    return formatSnakeCaseLabel(lookupName);
  }

  return trimmed;
}

export function resolveToolStepActionLabel(toolNames: string[]): string {
  const normalized = toolNames
    .map((name) => name.trim())
    .filter((name) => isDisplayableToolName(name));
  const unique = [...new Set(normalized)];
  if (unique.length === 0) return "Thinking";
  if (unique.length === 1) {
    if (normalized.length > 1) {
      return `Run ${normalized.length} tools in parallel`;
    }
    return resolveSingleToolStepActionLabel(unique[0]);
  }

  if (unique.every((name) => fileToolNames.has(name))) return "Handle files";
  if (unique.every((name) => dataToolNames.has(name))) return "Analyze data";
  if (unique.every((name) => workspaceToolNames.has(name))) return "Operate workspace";

  const firstKnown = unique.find((name) => toolActionLabels[name]);
  if (firstKnown && unique.length === 2) {
    return `${toolActionLabels[firstKnown]} etc.`;
  }
  return `Call ${unique.length} tools`;
}

function firstSummaryLine(text: string): string {
  const line = text.split("\n").find((segment) => segment.trim().length > 0) ?? "";
  return line
    .replace(/^[#>*\-\s]+/, "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .trim();
}

function looksChinese(text: string): boolean {
  return /[\u4e00-\u9fff]/.test(text);
}

function truncatePreview(text: string, max = SUMMARY_PREVIEW_MAX): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function buildThoughtPreview(content: string): string | undefined {
  const line = firstSummaryLine(content);
  if (!line) return undefined;
  return truncatePreview(line);
}

/** Step card subtitle — prefer Chinese tool labels over English LLM body text. */
export function resolveStepSummaryText(input: {
  content: string;
  hasToolCalls: boolean;
  displayToolNames: string;
  toolActionLabel: string;
  isThought: boolean;
}): string {
  const thoughtPreview = buildThoughtPreview(input.content);
  const toolPart = input.hasToolCalls
    ? input.displayToolNames
      ? `Call ${input.displayToolNames}`
      : input.toolActionLabel
    : undefined;

  if (toolPart) {
    if (thoughtPreview) {
      if (
        thoughtPreview === toolPart ||
        thoughtPreview === input.toolActionLabel ||
        thoughtPreview === input.displayToolNames
      ) {
        return toolPart;
      }
      return `${thoughtPreview} · ${toolPart}`;
    }
    return toolPart;
  }

  if (input.isThought) {
    if (thoughtPreview && looksChinese(thoughtPreview)) {
      return thoughtPreview;
    }
    if (thoughtPreview) {
      return thoughtPreview;
    }
    return "Thinking";
  }
  if (thoughtPreview && looksChinese(thoughtPreview)) {
    return thoughtPreview;
  }
  return thoughtPreview ? "Thinking" : "Step";
}

export function resolveCollaborationCompletedStepLabel(
  toolNames: string[],
  linkedToolName?: string,
): string {
  const normalized = new Set(toolNames.map((name) => name.trim()).filter(Boolean));
  if (linkedToolName) normalized.add(linkedToolName);
  if (normalized.has("submit_plan")) return "Plan approved";
  if (normalized.has("ask_user")) return "Confirmation complete";
  return "Collaboration complete";
}

export function resolveCollaborationStepLabel(
  toolNames: string[],
  isActive: boolean,
  linkedToolName?: string,
): string {
  const normalized = new Set(toolNames.map((name) => name.trim()).filter(Boolean));
  if (linkedToolName) normalized.add(linkedToolName);
  if (normalized.has("submit_plan")) return "Plan approval";
  if (normalized.has("ask_user")) return isActive ? "Waiting for user choice" : "User collaboration";
  return isActive ? "Waiting for confirmation" : "Collaboration step";
}

export function resolveStepBadgePresentation({
  stepNumber,
  isFinalAnswer,
  isStreamingAnswer,
  isActive,
  isThought,
  isCollaboration,
  isWaitingForUser,
}: {
  stepNumber: number;
  isFinalAnswer: boolean;
  isStreamingAnswer: boolean;
  isActive: boolean;
  isThought?: boolean;
  isCollaboration?: boolean;
  isWaitingForUser?: boolean;
}):
  | { kind: "number"; value: number }
  | { kind: "waiting" }
  | { kind: "collaboration" }
  | { kind: "final" }
  | { kind: "streaming" }
  | { kind: "thought" }
  | { kind: "empty" } {
  if (stepNumber > 0) return { kind: "number", value: stepNumber };
  if (isWaitingForUser || (isCollaboration && isActive)) return { kind: "waiting" };
  if (isCollaboration) return { kind: "collaboration" };
  if (isFinalAnswer) return { kind: "final" };
  if (isStreamingAnswer) return { kind: "streaming" };
  if (isThought) return { kind: "thought" };
  return { kind: "empty" };
}
