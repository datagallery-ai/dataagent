import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { existsSync } from 'node:fs';
import { chmod, mkdir, mkdtemp, open, readFile, unlink, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseEnv } from 'node:util';
import type { PromptFn } from './auth/interactive-login.js';
import { TuiAuthClient } from './auth/auth-client.js';
import { TuiCookieJar } from './auth/cookie-jar.js';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const pause = (ms: number) => new Promise<void>((done) => setTimeout(done, ms));

export async function prepareLocalConfig(directory: string, prompt: PromptFn, environment = process.env) {
  await mkdir(directory, { recursive: true, mode: 0o700 });
  await chmod(directory, 0o700);
  const configFile = join(directory, 'model.json');
  const saved = existsSync(configFile) ? JSON.parse(await readFile(configFile, 'utf8')) : {};
  const repo = existsSync(join(root, '.env')) ? parseEnv(await readFile(join(root, '.env'), 'utf8')) : {};
  const env: NodeJS.ProcessEnv = { ...repo, ...saved, ...environment };
  let changed = false;
  for (const [key, label, fallback] of [
    ['LLM_PROVIDER', 'Provider', 'openai'], ['LLM_MODEL', 'Model', ''],
    ['LLM_BASE_URL', 'Base URL', 'https://api.openai.com/v1'], ['LLM_API_KEY', 'API Key', ''],
  ]) {
    if (env[key!]?.trim()) continue;
    const value = key === 'LLM_API_KEY'
      ? await prompt.password('API Key (hidden): ')
      : await prompt.question(`${label}${fallback ? ` [${fallback}]` : ''}: `);
    env[key!] = value.trim() || fallback;
    if (!env[key!]) throw new Error(`${label} is required.`);
    changed = true;
  }
  if (changed) {
    const model = Object.fromEntries(['LLM_PROVIDER', 'LLM_MODEL', 'LLM_BASE_URL', 'LLM_API_KEY'].map((key) => [key, env[key]]));
    await writeFile(configFile, JSON.stringify(model, null, 2), { mode: 0o600 });
    await chmod(configFile, 0o600);
  }
  return env;
}

export async function startLocalRuntime(options: { prompt: PromptFn; stdout: NodeJS.WritableStream; directory?: string }) {
  const directory = resolve(options.directory ?? process.env.DATAFOUNDRY_TUI_HOME ?? join(homedir(), '.datafoundry', 'tui'));
  const env = await prepareLocalConfig(directory, options.prompt);
  const secretFile = join(directory, 'secret');
  try { await writeFile(secretFile, randomBytes(32).toString('hex'), { flag: 'wx', mode: 0o600 }); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error; }
  const secret = await readFile(secretFile, 'utf8');
  const token = randomBytes(32).toString('hex');
  const csrf = randomBytes(32).toString('hex');
  const runDirectory = await mkdtemp(join(directory, 'run-'));
  const readyFile = join(runDirectory, 'ready.json');
  const logPath = join(runDirectory, 'backend.log');
  const log = await open(logPath, 'a', 0o600);
  options.stdout.write(`Starting local DataAgent…\nData: ${directory}\nLog: ${logPath}\n`);
  const child = spawn('uv', ['run', '--directory', join(root, 'apps/api'), 'python', '-m', 'datafoundry_api.local_tui'], {
    cwd: root, detached: process.platform !== 'win32', stdio: ['ignore', log.fd, log.fd],
    env: {
      ...env, UV_NO_ENV_FILE: '1', API_HOST: '127.0.0.1', API_PORT: '8787', API_RELOAD: '0',
      AUTH_PUBLIC_BASE_URL: 'http://127.0.0.1', AUTH_REGISTRATION_MODE: 'closed', AUTH_EMAIL_DELIVERY: 'test',
      AUTH_DISABLED: 'false', AUTH_SESSION_SECRET: secret, SECRET_MASTER_KEY: secret,
      STORAGE_ROOT_DIR: join(directory, 'storage'), METADATA_DB_PATH: join(directory, 'metadata.sqlite'),
      LANGGRAPH_CHECKPOINT_DB_PATH: join(directory, 'checkpoints.sqlite'), LANGGRAPH_STORE_DB_PATH: join(directory, 'store.sqlite'),
      DATAAGENT_HOME: join(directory, 'dataagent'),
      TUI_LOCAL_SESSION_TOKEN: token, TUI_LOCAL_CSRF_TOKEN: csrf, TUI_LOCAL_READY_FILE: readyFile,
    },
  });
  let spawnError: Error | undefined;
  child.on('error', (error) => { spawnError = error; });
  let stopped = false;
  const signalChild = (signal: NodeJS.Signals) => {
    if (!child.pid) return;
    try {
      if (process.platform === 'win32') child.kill(signal);
      else process.kill(-child.pid, signal);
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw error; }
  };
  const emergencyStop = () => signalChild('SIGTERM');
  const terminate = () => { void stop().finally(() => {
    if (process.stdin.isTTY) process.stdin.setRawMode(false);
    process.exit(143);
  }); };
  async function stop() {
    if (stopped) return;
    stopped = true;
    process.removeListener('exit', emergencyStop);
    process.removeListener('SIGTERM', terminate);
    signalChild('SIGTERM');
    const deadline = Date.now() + 3000;
    while (child.exitCode === null && child.signalCode === null && !spawnError && Date.now() < deadline) await pause(50);
    signalChild('SIGKILL');
    await log.close();
    await unlink(readyFile).catch(() => {});
  }
  process.once('exit', emergencyStop);
  process.once('SIGTERM', terminate);
  try {
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      if (spawnError || child.exitCode !== null || child.signalCode !== null) {
        throw new Error(`Local backend failed to start. Check uv/Python dependencies and ${logPath}`);
      }
      if (existsSync(readyFile)) {
        const { port } = JSON.parse(await readFile(readyFile, 'utf8'));
        const baseUrl = `http://127.0.0.1:${port}`;
        try {
          const cookieJar = new TuiCookieJar();
          cookieJar.replace({ df_session: token, df_csrf: csrf });
          const auth = new TuiAuthClient({ apiBaseUrl: baseUrl, cookieJar, timeoutMs: 500 });
          const me = await auth.me();
          if (me.id === 'local-tui') return { runtimeUrl: `${baseUrl}/api/copilotkit`, token, csrf, stop, logPath };
        } catch { /* The socket is bound before application startup completes. */ }
      }
      await pause(100);
    }
    throw new Error(`Local backend startup timed out. See ${logPath}`);
  } catch (error) {
    await stop();
    throw error;
  }
}
