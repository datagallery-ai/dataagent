import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { MIN_NODE_VERSION, isSupportedNodeVersion } from "../platform-hints.mjs";
import { ensureDependencies, inspectDependencies } from "./dependencies.mjs";

function runner(map) {
  return async (command, args = []) => {
    const key = [command, ...args].join(" ");
    if (!(key in map) && !(command in map)) {
      const error = new Error(`missing mock for ${key}`);
      error.code = "ENOENT";
      throw error;
    }
    const value = map[key] ?? map[command];
    if (typeof value === "function") return value(args);
    if (value instanceof Error) throw value;
    return value;
  };
}

test("all workspace manifests and lock metadata declare the same Node minimum", () => {
  assert.equal(MIN_NODE_VERSION, "22.12.0");
  const lock = JSON.parse(readFileSync(new URL("../../package-lock.json", import.meta.url), "utf8"));
  for (const member of ["", "apps/tui", "apps/web"]) {
    const manifest = JSON.parse(readFileSync(new URL(`../../${member ? member + "/" : ""}package.json`, import.meta.url), "utf8"));
    assert.equal(manifest.engines.node, `>=${MIN_NODE_VERSION}`);
    assert.equal(lock.packages[member].engines.node, manifest.engines.node);
  }
  assert.equal(isSupportedNodeVersion("22.12.0"), true);
  assert.equal(isSupportedNodeVersion("22.11.9"), false);
});

test("requires Node 22.12.0, npm, and uv", async () => {
  const entries = await inspectDependencies({
    run: runner({
      "node --version": { stdout: "v22.14.0\n" },
      "npm --version": { stdout: "10.9.0\n" },
      "uv --version": { stdout: "uv 0.8.0\n" }
    })
  });
  assert.equal(entries.find((e) => e.name === "node").status, "ok");
  assert.equal(entries.find((e) => e.name === "npm").status, "ok");
  assert.equal(entries.find((e) => e.name === "uv").status, "ok");
  assert.equal(entries.find((e) => e.name === "python"), undefined);
});

for (const [version, expected] of [["v20.19.0", "missing"], ["v22.0.0", "missing"], ["v22.11.9", "missing"], ["v22.12.0", "ok"], ["v22.12.1", "ok"], ["v24.0.0", "ok"]]) {
  test(`dependency inspection reports ${version} as ${expected}`, async () => {
    const entries = await inspectDependencies({ run: runner({
      "node --version": { stdout: version },
      "npm --version": { stdout: "10.9.0" },
      "uv --version": { stdout: "uv 0.8.0" },
    }) });
    assert.equal(entries[0].minimumVersion, "22.12.0");
    assert.equal(entries[0].status, expected);
  });
}

test("missing uv fails before installing missing Node or npm", async () => {
  await assert.rejects(
    () => ensureDependencies({
      nonInteractive: true,
      uid: 0,
      ask: async () => assert.fail("must not prompt"),
      run: runner({
        "node --version": { stdout: "v22.11.9\n" },
        "npm --version": Object.assign(new Error("npm missing"), { code: "ENOENT" }),
        "uv --version": Object.assign(new Error("uv missing"), { code: "ENOENT" })
      }),
      install: async () => assert.fail("must not install uv")
    }),
    /uv/
  );
});

test("missing Node and npm share one installer invocation", async () => {
  const installs = [];
  let installed = false;
  await ensureDependencies({
    nonInteractive: true, uid: 0, print: () => {},
    run: async command => {
      if (command === "uv") return { stdout: "uv 0.8.0" };
      if (command === "node") return { stdout: installed ? "v22.12.0" : "v22.11.9" };
      if (command === "npm" && installed) return { stdout: "10.9.0" };
      throw new Error("missing npm");
    },
    install: async action => { installs.push(action); installed = true; },
  });
  assert.deepEqual(installs, ["node"]);
});

test("interactive refusal returns corrective install command", async () => {
  await assert.rejects(
    () => ensureDependencies({
      nonInteractive: false,
      ask: async () => "n",
      run: runner({
        "node --version": { stdout: "v20.0.0\n" },
        "npm --version": { stdout: "10.0.0\n" },
        "uv --version": { stdout: "uv 0.8.0\n" }
      }),
      install: async () => assert.fail("must not install")
    }),
    /install-dependency\.sh node|Node\.js 22/
  );
});

test("non-interactive install requires root or passwordless sudo", async () => {
  await assert.rejects(
    () => ensureDependencies({
      nonInteractive: true,
      uid: 1000,
      ask: async () => assert.fail("must not prompt"),
      run: runner({
        "node --version": { stdout: "v20.0.0\n" },
        "npm --version": { stdout: "10.0.0\n" },
        "uv --version": { stdout: "uv 0.8.0\n" },
        "sudo -n true": Object.assign(new Error("sudo failed"), { code: 1 })
      }),
      install: async () => assert.fail("must not install")
    }),
    /root|passwordless sudo|non-interactive/i
  );
});

test("non-interactive install passes --non-interactive to install-dependency.sh", async () => {
  const installs = [];
  let nodeCalls = 0;
  await ensureDependencies({
    nonInteractive: true,
    uid: 0,
    ask: async () => assert.fail("must not prompt"),
    run: async (command, args = []) => {
      const key = [command, ...args].join(" ");
      if (key === "node --version") {
        nodeCalls += 1;
        return nodeCalls === 1 ? { stdout: "v20.0.0\n" } : { stdout: "v22.14.0\n" };
      }
      if (key === "npm --version") return { stdout: "10.9.0\n" };
      if (key === "uv --version") return { stdout: "uv 0.8.0\n" };
      throw new Error(`missing mock for ${key}`);
    },
    install: async (action, installOptions) => {
      installs.push({ action, installOptions });
    }
  });
  assert.equal(installs.length, 1);
  assert.equal(installs[0].action, "node");
  assert.equal(installs[0].installOptions.nonInteractive, true);
});
