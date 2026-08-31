import assert from "node:assert/strict";
import test from "node:test";
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

test("requires Node 22 and npm only", async () => {
  const entries = await inspectDependencies({
    run: runner({
      "node --version": { stdout: "v22.14.0\n" },
      "npm --version": { stdout: "10.9.0\n" }
    })
  });
  assert.equal(entries.find((e) => e.name === "node").status, "ok");
  assert.equal(entries.find((e) => e.name === "npm").status, "ok");
  assert.equal(entries.find((e) => e.name === "python"), undefined);
  assert.equal(entries.find((e) => e.name === "uv"), undefined);
});

test("interactive refusal returns corrective install command", async () => {
  await assert.rejects(
    () => ensureDependencies({
      nonInteractive: false,
      ask: async () => "n",
      run: runner({
        "node --version": { stdout: "v20.0.0\n" },
        "npm --version": { stdout: "10.0.0\n" }
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
