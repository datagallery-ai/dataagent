import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { chmod, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { isSupportedNodeVersion, MIN_NODE_VERSION } from "../platform-hints.mjs";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "../..");
const DEPLOY_SH = path.join(ROOT, "deploy.sh");
const INSTALL_SH = path.join(ROOT, "scripts/deploy/install-dependency.sh");

function runBash(args, options = {}) {
  return spawnSync("bash", args, {
    cwd: ROOT,
    encoding: "utf8",
    env: { ...process.env, BASH_ENV: "", ...(options.env ?? {}) },
    input: options.input,
    timeout: options.timeout ?? 10_000
  });
}

test("shell and JavaScript agree on the complete Node version boundary", () => {
  for (const [version, supported] of [["v22.11.9", false], ["v22.12.0", true], ["v22.12.1", true], ["v24.0.0", true], ["v22.12.0-rc.1", false], ["invalid", false]]) {
    const result = runBash(["-c", 'source "$1"; echo "$MIN_NODE_VERSION"; node_version_supported "$2"', "fixture", DEPLOY_SH, version]);
    assert.equal(result.stdout.trim(), MIN_NODE_VERSION);
    assert.equal(result.status, supported ? 0 : 1, version);
    assert.equal(isSupportedNodeVersion(version), supported, version);
  }
});

async function makeFakePath(binaries) {
  const dir = await mkdtemp(path.join(os.tmpdir(), "datagallery-path-"));
  for (const [name, body] of Object.entries(binaries)) {
    const filePath = path.join(dir, name);
    await writeFile(filePath, body, { mode: 0o755 });
    await chmod(filePath, 0o755);
  }
  return dir;
}

test("./deploy.sh help delegates without installing when Node 22.12.0+ exists", () => {
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

test("working Node can run help on openEuler and unknown distributions", async (t) => {
  const dir = await mkdtemp(path.join(os.tmpdir(), "datagallery-os-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const environment = path.join(dir, "bash-env");
  await writeFile(environment, `
source() {
  if [[ "$1" == "/etc/os-release" ]]; then ID="$FIXTURE_DISTRO";
  else builtin source "$@"; fi
}
uname() { echo fixture-architecture; }
`);
  for (const distro of ["openEuler", "unknown-linux"]) {
    const result = runBash([DEPLOY_SH, "help"], {
      env: { BASH_ENV: environment, FIXTURE_DISTRO: distro }
    });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /deploy\s+Configure/);
  }
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

for (const command of ["start", "restart", "tui"]) {
  for (const version of ["v20.1.0", "v22.0.0", "v22.11.9"]) {
    test(`${command} rejects ${version} without installing or prompting`, async (t) => {
      const fakePath = await makeFakePath({ node: `#!/bin/bash\necho ${version}\n` });
      t.after(() => rm(fakePath, { recursive: true, force: true }));
      const result = runBash([DEPLOY_SH, command], { env: { PATH: `${fakePath}:/bin` } });
      assert.equal(result.status, 1);
      assert.match(result.stderr, /Node\.js 22\.12\.0\+ is required for this command/);
      assert.doesNotMatch(result.stdout, /Installer:|Install Node\.js/);
    });
  }
}

test("bootstrap distinguishes commands from option values", () => {
  for (const [args, skipsInstall] of [
    [["deploy", "--config", "start"], false],
    [["--config", "start"], false],
    [["--web-host", "restart"], false],
    [["--web-port", "3310", "start"], true],
    [["restart", "--config", "deploy"], true],
    [["deploy", "--web-host", "status"], false],
  ]) {
    const result = runBash(["-c", 'source "$1"; shift; command_skips_install "$@"', "fixture", DEPLOY_SH, ...args]);
    assert.equal(result.status, skipsInstall ? 0 : 1, JSON.stringify(args));
  }
});

test("interactive Node installation explains the installer before asking", async () => {
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
  assert.match(output, /NodeSource/);
  assert.match(output, /Node\.js 22\.12\.0\+ is required/);
  assert.match(output, /install-dependency\.sh node/);
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
  // The bootstrap delegates to the installer, which downloads scripts to a temp file.
  assert.match(await readFile(INSTALL_SH, "utf8"), /mktemp/);
});

async function installerFixture(t, manager, overrides = {}) {
  const log = String.raw`printf '%s\n' "$(basename "$0") $*" >> "$INSTALL_LOG"`;
  const directory = await makeFakePath({
    id: "#!/bin/bash\necho 1000\n",
    mktemp: '#!/bin/bash\n[[ "$*" != *--* ]] || exit 64\nexec mktemp-system "$@"\n',
    sudo: `#!/bin/bash\n${log}\nif [[ "$1" == "-n" ]]; then shift; fi\n"$@"\n`,
    curl: `#!/bin/bash\n${log}\n[[ "\${FAIL_DOWNLOAD:-0}" == 0 ]] || exit 22\nprintf 'printf "repository setup\\n" >> "$INSTALL_LOG"\\n' > "\${!#}"\n`,
    tee: `#!/bin/bash\n${log}\ncat > "$REPO_LOG"\n`,
    node: '#!/bin/bash\nif [[ -f "$INSTALL_DONE" ]]; then echo "${INSTALLED_NODE_VERSION:-v22.12.0}"; else echo v20.1.0; fi\n',
    npm: "#!/bin/bash\necho 10.0.0\n",
    ...(manager ? { [manager]: `#!/bin/bash\n${log}\n[[ "\${FAIL_INSTALL:-0}" == 0 ]] || { echo 'package install failed' >&2; exit 42; }\n: > "$INSTALL_DONE"\n` } : {}),
  });
  t.after(() => rm(directory, { recursive: true, force: true }));
  for (const command of ["bash", "cat", "basename", "dirname", "mktemp", "rm", "rmdir", "chmod"]) {
    const real = process.env.PATH.split(path.delimiter).map(dir => path.join(dir, command)).find(existsSync);
    assert(real, `Missing test prerequisite: ${command}`);
    await symlink(real, path.join(directory, command === "mktemp" ? "mktemp-system" : command));
  }
  const logPath = path.join(directory, "commands.log");
  await writeFile(logPath, "");
  return {
    env: { PATH: directory, INSTALL_LOG: logPath, INSTALL_DONE: path.join(directory, "installed"),
      REPO_LOG: path.join(directory, "repo"), ...overrides },
    log: () => readFile(logPath, "utf8"),
  };
}

for (const manager of ["apt-get", "dnf", "yum"]) {
  test(`custom Node package bypasses NodeSource with ${manager}`, async t => {
    const fixture = await installerFixture(t, manager, { DATAAGENT_NODE_PACKAGE_URL: "https://mirror.example/node/package" });
    const result = runBash([INSTALL_SH, "node", "--non-interactive"], fixture);
    assert.equal(result.status, 0, result.stderr);
    const log = await fixture.log();
    assert.match(log, /curl .*https:\/\/mirror\.example\/node\/package/);
    assert.match(log, new RegExp(`${manager} install -y .*\\.${manager === "apt-get" ? "deb" : "rpm"}`));
    assert.doesNotMatch(log, /nodesource|repository setup|tee/);
    assert.equal(existsSync(fixture.env.REPO_LOG), false);
  });
}

for (const manager of ["apt-get", "dnf", "yum"]) {
  for (const bootstrap of [false, true]) {
    test(`${bootstrap ? "bootstrap" : "installer"} installs Node through ${manager}`, async (t) => {
      const fixture = await installerFixture(t, manager);
      const args = bootstrap
        ? ["-c", `source "${DEPLOY_SH}"; ensure_node_22 deploy --non-interactive`]
        : [INSTALL_SH, "node", "--non-interactive"];
      const result = runBash(args, fixture);
      assert.equal(result.status, 0, result.stderr);
      const log = await fixture.log();
      assert.match(log, new RegExp(`sudo -n ${manager} install -y nodejs`));
      if (manager === "apt-get") assert.match(log, /https:\/\/deb\.nodesource\.com\/setup_22\.x/);
      else {
        const repo = await readFile(fixture.env.REPO_LOG, "utf8");
        assert.match(repo, /https:\/\/rpm\.nodesource\.com\/pub_22\.x\/nodistro\/nodejs\/\$basearch/);
        assert.match(repo, /gpgcheck=1/);
        assert.match(repo, /gpgkey=https:\/\/rpm\.nodesource\.com\/gpgkey\/ns-operations-public\.key/);
        assert.doesNotMatch(log, /deb\.nodesource|apt-get|setup_22/);
      }
    });
  }
}

test("missing package manager fails before attempting installation", async (t) => {
  const fixture = await installerFixture(t);
  const result = runBash([INSTALL_SH, "node", "--non-interactive"], fixture);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /no supported package manager.*apt-get.*dnf.*yum/i);
  assert.equal(await fixture.log(), "");
});

test("working Node does not require a package manager", async (t) => {
  const fixture = await installerFixture(t);
  const node = path.join(fixture.env.PATH, "node");
  await rm(node);
  await symlink(process.execPath, node);
  const result = runBash([DEPLOY_SH, "help"], fixture);
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /deploy\s+Configure/);
  assert.equal(await fixture.log(), "");
});

test("download failure stops bootstrap without starting the CLI", async (t) => {
  const fixture = await installerFixture(t, "apt-get", { FAIL_DOWNLOAD: "1" });
  const result = runBash(["-c", `source "${DEPLOY_SH}"; ensure_node_22 deploy --non-interactive; echo SHOULD_NOT_REACH`], fixture);
  assert.equal(result.status, 22, result.stderr);
  assert.doesNotMatch(result.stdout, /SHOULD_NOT_REACH/);
  assert.doesNotMatch(await fixture.log(), /apt-get install/);
});

test("package installation failure stops bootstrap without starting the CLI", async (t) => {
  const fixture = await installerFixture(t, "dnf", { FAIL_INSTALL: "1" });
  const result = runBash(["-c", `source "${DEPLOY_SH}"; ensure_node_22 deploy --non-interactive; echo SHOULD_NOT_REACH`], fixture);
  assert.equal(result.status, 42, result.stderr);
  assert.match(result.stderr, /package install failed/);
  assert.doesNotMatch(result.stdout, /SHOULD_NOT_REACH/);
});

for (const version of ["v20.1.0", "v22.11.9"]) {
  test(`installer rejects a package that leaves Node at ${version}`, async (t) => {
    const fixture = await installerFixture(t, "dnf", { INSTALLED_NODE_VERSION: version });
    const result = runBash([INSTALL_SH, "node", "--non-interactive"], fixture);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /Node\.js 22\.12\.0\+ is required after installation/);
  });
}
