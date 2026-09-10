import assert from 'node:assert/strict';
import { mkdtemp, readFile, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { it } from 'node:test';
import { prepareLocalConfig, startLocalRuntime } from './local-runtime.js';
import { bootstrapTuiAuth } from './auth/bootstrap.js';
import { runTui } from './index.js';

it('prompts only for missing model fields, masks key and saves private config', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'tui-config-'));
  const questions: string[] = [];
  const env = { LLM_PROVIDER: 'openai', LLM_MODEL: '', LLM_BASE_URL: 'http://localhost/v1', LLM_API_KEY: '' };
  const config = await prepareLocalConfig(dir, {
    question: async (q) => { questions.push(q); return 'test-model'; },
    password: async () => 'private-test-key', close() {},
  }, env);
  assert.equal(config.LLM_MODEL, 'test-model');
  assert.equal(questions.length, 1);
  assert.equal(JSON.parse(await readFile(join(dir, 'model.json'), 'utf8')).LLM_API_KEY, 'private-test-key');
  if (process.platform !== 'win32') assert.equal((await stat(join(dir, 'model.json'))).mode & 0o777, 0o600);
  const restored = await prepareLocalConfig(dir, {
    question: async () => { throw new Error('Unexpected prompt'); },
    password: async () => { throw new Error('Unexpected prompt'); }, close() {},
  }, {});
  assert.equal(restored.LLM_MODEL, 'test-model');
});

it('real local launcher starts, authenticates without registration, and stops its backend', {
  skip: !process.env.TUI_LOCAL_SMOKE, timeout: 90_000,
}, async () => {
  const dir = await mkdtemp(join(tmpdir(), 'tui-local-smoke-'));
  const local = await startLocalRuntime({ directory: dir, stdout: { write: () => true } as unknown as NodeJS.WritableStream,
    prompt: { question: async () => 'test', password: async () => 'test', close() {} } });
  const baseUrl = local.runtimeUrl.replace('/api/copilotkit', '');
  try {
    assert.equal((await fetch(`${baseUrl}/api/v1/me`)).status, 401);
    const auth = await bootstrapTuiAuth({ apiBaseUrl: baseUrl, localCredentials: local });
    assert.equal(auth.kind, 'authenticated');
    if (auth.kind !== 'authenticated') throw new Error('Unexpected login screen');
    assert.equal(auth.session.user.id, 'local-tui');
    const response = await auth.transport.fetch(`${baseUrl}/api/v1/workspace-config`);
    assert.equal(response.status, 200);
  } finally { await local.stop(); }
  await assert.rejects(fetch(`${baseUrl}/healthz`, { signal: AbortSignal.timeout(1000) }));
});

it('default TUI entry skips login and cleans up even when rendering fails', {
  skip: !process.env.TUI_LOCAL_SMOKE, timeout: 90_000,
}, async () => {
  const previous = process.env.DATAFOUNDRY_TUI_HOME;
  process.env.DATAFOUNDRY_TUI_HOME = await mkdtemp(join(tmpdir(), 'tui-entry-smoke-'));
  let url = '';
  try {
    const result = await runTui({ argv: [],
      fetchImpl: async (input, init) => { url = new URL(String(input)).origin; return fetch(input, init); },
      stdout: { write: () => true } as unknown as NodeJS.WritableStream,
      prompt: { question: async () => 'test', password: async () => 'test', close() {} },
      renderApp: async ({ configClient }) => {
        const catalog = await configClient.getWorkspaceConfig();
        assert.ok(catalog.modelProfiles.length > 0);
        throw new Error('Simulated render failure');
      },
    });
    assert.equal(result, 1);
    assert.ok(url);
    await assert.rejects(fetch(`${url}/healthz`, { signal: AbortSignal.timeout(1000) }));
  } finally {
    if (previous === undefined) delete process.env.DATAFOUNDRY_TUI_HOME;
    else process.env.DATAFOUNDRY_TUI_HOME = previous;
  }
});
