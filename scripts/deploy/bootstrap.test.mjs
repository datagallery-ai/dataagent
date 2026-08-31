import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmod, mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "../..");
const DEPLOY_SH = path.join(ROOT, "deploy.sh");
const INSTALL_SH = path.join(ROOT, "scripts/deploy/install-dependency.sh");

function runBash(args, options = {}) {
  return spawnSync("bash", args, {
    cwd: ROOT,
    encoding: "utf8",
    env: { ...process.env, ...(options.env ?? {}) },
    input: options.input,
    timeout: options.timeout ?? 10_000
  });
}

async function makeFakePath(binaries) {
  const dir = await mkdtemp(path.join(os.tmpdir(), "datafoundry-path-"));
  for (const [name, body] of Object.entries(binaries)) {
    const filePath = path.join(dir, name);
    await writeFile(filePath, body, { mode: 0o755 });
    await chmod(filePath, 0o755);
  }
  return dir;
}

test("./deploy.sh help delegates without installing when Node 22+ exists", () => {
  const result = runBash([DEPLOY_SH, "help"]);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /deploy\s+Configure/);
  assert.match(result.stdout, /\bstart\b/);
  assert.match(result.stdout, /\bstop\b/);
  assert.match(result.stdout, /\brestart\b/);
  assert.match(result.stdout, /\bstatus\b/);
  assert.match(result.stdout, /\blogs\b/);
  assert.match(result.stdout, /\bdoctor\b/);
  assert.match(result.stdout, /\bhelp\b/);
  assert.doesNotMatch(result.stdout + result.stderr, /Install Node\.js|nodesource|apt-get install/i);
});

test("unsupported OS exits 1 with a precise message", async () => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "datafoundry-os-"));
  const osRelease = path.join(dir, "os-release");
  await writeFile(osRelease, 'ID=fedora\nVERSION_ID="40"\n');
  const result = runBash([DEPLOY_SH, "help"], {
    env: { DATAFOUNDRY_OS_RELEASE_FILE: osRelease }
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Unsupported operating system: fedora/);
});

test("unsupported architecture exits 1 with a precise message", async () => {
  const result = runBash([DEPLOY_SH, "help"], {
    env: { DATAFOUNDRY_UNAME_M: "ppc64le" }
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /Unsupported architecture: ppc64le/);
});

test("Node 20 is rejected before CLI starts", async () => {
  const fakePath = await makeFakePath({
    node: "#!/bin/bash\necho v20.11.0\n",
    npm: "#!/bin/bash\necho 10.0.0\n",
    curl: "#!/bin/bash\necho curl-should-not-run >&2\nexit 99\n",
    sudo: "#!/bin/bash\nexit 1\n",
    id: "#!/bin/bash\necho 1000\n"
  });
  const script = `
set -euo pipefail
export PATH="${fakePath}:/bin"
hash -r
command -v node
node --version
source "${DEPLOY_SH}"
ensure_node_22 deploy --non-interactive
echo SHOULD_NOT_REACH
`;
  const result = runBash(["-c", script], {
    env: {
      PATH: `${fakePath}:/bin`,
      HOME: os.tmpdir()
    },
    input: ""
  });
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`;
  assert.notEqual(result.status, 0, output);
  assert.match(output, /Unsupported Node\.js version|Node\.js 22|passwordless sudo|requires root/i);
  assert.doesNotMatch(output, /SHOULD_NOT_REACH/);
  assert.doesNotMatch(output, /curl-should-not-run/);
});

test("interactive Node installation prints repository and command, then asks once", async () => {
  const fakePath = await makeFakePath({
    curl: "#!/bin/bash\nexit 99\n",
    sudo: "#!/bin/bash\nexit 1\n",
    id: "#!/bin/bash\necho 1000\n"
  });
  const result = runBash(
    ["-c", `source "${DEPLOY_SH}"; install_node_22 deploy`],
    {
      env: {
        PATH: `${fakePath}:/bin:/usr/bin`,
        HOME: os.tmpdir()
      },
      input: "n\n"
    }
  );
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`;
  assert.notEqual(result.status, 0, output);
  assert.match(output, /deb\.nodesource\.com\/setup_22\.x/);
  assert.match(output, /Node\.js 22 is required/);
  assert.match(output, /apt-get install -y nodejs/);
  assert.match(output, /re-run \.\/deploy\.sh/);
});

test("--non-interactive never reads stdin and fails without root/passwordless sudo", async () => {
  const fakePath = await makeFakePath({
    id: "#!/usr/bin/env bash\necho 1000\n",
    sudo: "#!/usr/bin/env bash\nexit 1\n",
    curl: "#!/usr/bin/env bash\necho unexpectedly-called >&2; exit 99\n"
  });
  const result = runBash(
    ["-c", `source "${DEPLOY_SH}"; install_node_22 deploy --non-interactive; echo SHOULD_NOT_REACH`],
    {
      env: {
        PATH: `${fakePath}:/bin:/usr/bin`,
        HOME: os.tmpdir()
      },
      input: "y\ny\ny\n"
    }
  );
  const output = `${result.stdout ?? ""}${result.stderr ?? ""}`;
  assert.equal(result.status, 1, output);
  assert.match(output, /root or passwordless sudo/i);
  assert.doesNotMatch(output, /unexpectedly-called|SHOULD_NOT_REACH/);
});

test("installer accepts only node", () => {
  const bad = runBash([INSTALL_SH, "ruby"]);
  assert.equal(bad.status, 2);
  assert.match(bad.stderr, /Usage: install-dependency\.sh <node>/);
});

test("no code path executes curl | bash", async () => {
  const bootstrap = path.join(ROOT, "scripts/deploy/bootstrap.sh");
  const sources = [
    await readFile(DEPLOY_SH, "utf8"),
    await readFile(bootstrap, "utf8"),
    await readFile(INSTALL_SH, "utf8")
  ];
  for (const source of sources) {
    assert.doesNotMatch(source, /curl[^\n]*\|\s*(bash|sh)/);
  }
  // Node bootstrap and dependency installer download to a temp file first.
  assert.match(await readFile(bootstrap, "utf8"), /mktemp/);
  assert.match(await readFile(INSTALL_SH, "utf8"), /mktemp/);
});
