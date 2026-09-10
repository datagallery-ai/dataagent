import type { Capabilities, ConfigClient, RunDefaults, WorkspaceConfig } from './config/index.js';
import type { WorkspaceConfigItem, WorkspaceConfigStore } from './state/data-task-state.js';

/** Replace local demo/stale resource IDs with the authenticated server catalog. */
export async function loadBackendConfig(client: ConfigClient) {
  const [catalog, defaults, capabilities] = await Promise.all([
    client.getWorkspaceConfig(), client.getRunDefaults(), client.getCapabilities(),
  ]);
  return { defaults, capabilities, workspace: workspaceFromBackend(catalog, defaults, capabilities) };
}

export function workspaceFromBackend(
  catalog: WorkspaceConfig, defaults: RunDefaults, capabilities: Capabilities,
): WorkspaceConfigStore {
  const item = (resource: { id: string; name: string; description: string }, enabled: boolean): WorkspaceConfigItem => ({
    id: resource.id, name: resource.name, description: resource.description, enabled,
  });
  return {
    db: capabilities['runtime.dataTools'] ? catalog.datasources.map((r) => ({
      ...item(r, defaults.enabledDatasourceIds.includes(r.id)), settings: { type: r.type, datasourceId: r.id },
    })) : [],
    kb: capabilities.knowledge ? catalog.knowledgeBases.map((r) => item(r, defaults.enabledKnowledgeIds.includes(r.id))) : [],
    llm: catalog.modelProfiles.map((r) => ({
      ...item(r, r.id === defaults.activeLlmProfileId), settings: { modelName: r.modelName, provider: r.provider },
    })),
    skill: capabilities.skills ? catalog.skills.map((r) => ({
      ...item(r, defaults.enabledSkillIds.includes(r.id)), settings: { packageFormat: r.packageFormat },
    })) : [],
    mcp: capabilities.mcp ? catalog.mcpServers.map((r) => item(r, defaults.enabledMcpServerIds.includes(r.id))) : [],
  };
}

export function commandAvailable(command: string, capabilities: Capabilities): boolean {
  const name = command.trim().replace(/^\//, '').split(/\s+/)[0];
  if (name === 'resume') return capabilities['conversation.memory'] === true;
  if (name === 'datasource' || name === 'ds') return capabilities['runtime.dataTools'] === true;
  if (name === 'skill' || name === 'skills') return capabilities.skills === true;
  return true;
}
