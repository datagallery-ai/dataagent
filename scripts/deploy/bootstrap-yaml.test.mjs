import assert from 'node:assert/strict';
import { cpSync, mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { createRequire } from 'node:module';
import { join, dirname, delimiter } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import test from 'node:test';

for (const existingConfig of [false, true, 'install-completes']) test(`deployment bootstraps YAML without node_modules (existing config: ${existingConfig})`, t => {
  if (process.platform === 'win32') return t.skip('deploy.sh requires bash');
  const root = mkdtempSync(join(tmpdir(), 'deploy-no-modules-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const source = fileURLToPath(new URL('../../', import.meta.url));
  cpSync(join(source, 'scripts'), join(root, 'scripts'), { recursive: true });
  cpSync(join(source, 'deploy.sh'), join(root, 'deploy.sh'));
  writeFileSync(join(root, 'package.json'), '{"type":"module"}');
  mkdirSync(join(root, 'home'));
  if (existingConfig) writeFileSync(join(root, 'home/config.yaml'), 'server:\n  host: 127.0.0.1\n  port: 8790\n');
  mkdirSync(join(root, 'bin'));
  // Stop at installation, without network or changes to the real checkout.
  writeFileSync(join(root, 'bin/npm'), '#!/bin/sh\necho "reached npm $*" >&2\nexit 42\n', { mode: 0o755 });
  if (existingConfig === 'install-completes') {
    writeFileSync(join(root, 'home/config.yaml'), 'server: false\n');
    const yamlPackage = dirname(createRequire(import.meta.url).resolve('yaml/package.json'));
    writeFileSync(join(root, 'bin/npm'), `#!${process.execPath}
import fs from 'node:fs';
fs.mkdirSync(${JSON.stringify(join(root, 'node_modules'))}, {recursive:true});
fs.cpSync(${JSON.stringify(yamlPackage)}, ${JSON.stringify(join(root, 'node_modules/yaml'))}, {recursive:true});
`, { mode: 0o755 });
  }
  const env = { ...process.env, DATAAGENT_HOME: join(root, 'home'), PATH: `${join(root, 'bin')}${delimiter}${process.env.PATH}`, NODE_PATH: '' };
  const help = spawnSync('bash', ['deploy.sh', 'help'], { cwd: root, env, encoding: 'utf8' });
  assert.equal(help.status, 0, help.stderr);
  const deploy = spawnSync('bash', ['deploy.sh', 'deploy', '--non-interactive'], { cwd: root, env, encoding: 'utf8' });
  assert.equal(deploy.status, 1);
  assert.match(deploy.stdout + deploy.stderr, existingConfig === 'install-completes' ? /invalid server mapping/ : /reached npm ci/);
  assert.doesNotMatch(deploy.stdout + deploy.stderr, /ERR_MODULE_NOT_FOUND|Cannot find package/);
});
