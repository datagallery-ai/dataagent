import type { BackendCapabilitiesResponse } from "./types";
import type { BackendCapability } from "../../app/data-tasks/data-task-state";

export type RuntimeCapability = "conversationMemory" | "knowledge" | "mcp" | "skills";

const DEFAULT_BACKEND_CAPABILITIES: Record<BackendCapability, boolean> = {
  "datasource.server": false,
  "datasource.queryPolicy": false,
  "llm.samplingParams": false,
  "artifact.export": true,
  "artifact.list": false,
  "artifact.promote": false,
  "chat.imageInput": false,
  "chat.fileUpload": false,
  "conversation.title": false,
  "interaction.resume": false,
  files: false,
};

const DEFAULT_RUNTIME_CAPABILITIES: Record<RuntimeCapability, boolean> = {
  conversationMemory: false,
  knowledge: false,
  mcp: false,
  skills: false,
};

let backendCapabilities: Record<BackendCapability, boolean> = {
  ...DEFAULT_BACKEND_CAPABILITIES,
};

let runtimeCapabilities: Record<RuntimeCapability, boolean> = {
  ...DEFAULT_RUNTIME_CAPABILITIES,
};

export function applyBackendCapabilities(
  response: BackendCapabilitiesResponse,
): Record<BackendCapability, boolean> {
  backendCapabilities = {
    "datasource.server": response["datasource.server"] ?? false,
    "datasource.queryPolicy": response["datasource.queryPolicy"] ?? false,
    "llm.samplingParams": response["llm.samplingParams"] ?? false,
    "artifact.export": response["artifact.export"] ?? true,
    "artifact.list": response["artifact.list"] ?? false,
    "artifact.promote": response["artifact.promote"] ?? false,
    "chat.imageInput": response["chat.imageInput"] ?? false,
    "chat.fileUpload": response["chat.fileUpload"] ?? false,
    "conversation.title": response["conversation.title"] ?? false,
    "interaction.resume": response["interaction.resume"] ?? false,
    files: response.files ?? false,
  };
  runtimeCapabilities = {
    conversationMemory: response["conversation.memory"] ?? false,
    knowledge: response.knowledge ?? false,
    mcp: response.mcp ?? false,
    skills: response.skills ?? false,
  };
  return backendCapabilities;
}

export function getBackendCapabilities(): Record<BackendCapability, boolean> {
  return backendCapabilities;
}

export function getRuntimeCapabilities(): Record<RuntimeCapability, boolean> {
  return runtimeCapabilities;
}

export function resetCapabilitiesForTests(): void {
  backendCapabilities = { ...DEFAULT_BACKEND_CAPABILITIES };
  runtimeCapabilities = { ...DEFAULT_RUNTIME_CAPABILITIES };
}

export function isResourcePanelSupported(
  kind: "kb" | "mcp" | "skill",
): boolean {
  if (kind === "kb") return runtimeCapabilities.knowledge;
  if (kind === "mcp") return runtimeCapabilities.mcp;
  return runtimeCapabilities.skills;
}
