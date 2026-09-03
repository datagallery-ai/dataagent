/**
 * Deep Agents emits `ls` / `execute` / `glob` / `task`.
 * The workbench vocabulary still uses the previous Runtime names for labels
 * and result cards. Canonicalize at the display edge; do not rename the wire.
 */
export const TOOL_NAME_ALIASES: Record<string, string> = {
  ls: "list_files",
  execute: "execute_command",
};

const WORKSPACE_CANONICAL_TOOLS = new Set([
  "read_file",
  "edit_file",
  "write_file",
  "list_files",
  "glob",
  "grep",
  "mkdir",
  "file_stat",
  "execute_command",
  "promote_workspace_file",
]);

export function stripMcpToolPrefix(toolName: string): string {
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

export function canonicalizeToolName(toolName: string): string {
  const lookup = stripMcpToolPrefix(toolName);
  return TOOL_NAME_ALIASES[lookup] ?? lookup;
}

export function isWorkspaceCanonicalTool(toolName: string): boolean {
  return WORKSPACE_CANONICAL_TOOLS.has(canonicalizeToolName(toolName));
}
