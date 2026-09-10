import assert from 'node:assert/strict';
import { it } from 'node:test';
import { ConfigClient } from './config/index.js';
import { commandAvailable, loadBackendConfig } from './backend-config.js';

it('loads the Python flat capability map and replaces local resources with server selections', async () => {
  const common = { name: 'resource', description: '', defaultEnabled: true, builtin: false, revision: 1, createdAt: '', updatedAt: '' };
  const calls: string[] = [];
  const responses: Record<string, unknown> = {
    '/api/v1/capabilities': { skills: true, mcp: true, knowledge: false, 'runtime.dataTools': false, 'conversation.memory': false },
    '/api/v1/run-defaults': {
      enabledDatasourceIds: [], enabledKnowledgeIds: [], enabledMcpServerIds: ['mcp-1'],
      enabledSkillIds: ['skill-2'], activeSkillId: 'skill-2', activeLlmProfileId: 'model-2',
    },
    '/api/v1/workspace-config': {
      datasources: [], knowledgeBases: [],
      modelProfiles: [1, 2].map(n => ({ ...common, id: `model-${n}`, modelName: `model${n}`, secretRef: null })),
      skills: [1, 2].map(n => ({ ...common, id: `skill-${n}`, packageFormat: 'skill-md' })),
      mcpServers: [{ ...common, id: 'mcp-1', transport: 'streamable-http', serverUrl: 'http://localhost/mcp', secretRef: null }],
    },
  };
  const config = await loadBackendConfig(new ConfigClient({
    baseUrl: 'http://localhost', fetchImpl: async (url) => {
      const path = new URL(String(url)).pathname;
      calls.push(path);
      assert.ok(path in responses, path);
      return Response.json({ success: true, data: responses[path] });
    },
  }));
  assert.equal(calls.length, 3);
  assert.deepEqual(config.workspace.db, []);
  assert.deepEqual(config.workspace.skill.filter(r => r.enabled).map(r => r.id), ['skill-2']);
  assert.deepEqual(config.workspace.llm.filter(r => r.enabled).map(r => r.id), ['model-2']);
  assert.deepEqual(config.workspace.mcp.filter(r => r.enabled).map(r => r.id), ['mcp-1']);
  assert.equal(commandAvailable('/resume latest', config.capabilities), false);
  assert.equal(commandAvailable('/ds', config.capabilities), false);
  assert.equal(commandAvailable('/skill list', config.capabilities), true);
});
