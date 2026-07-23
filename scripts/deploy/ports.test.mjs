import assert from "node:assert/strict";
import test from "node:test";
import {
  parsePort,
  selectDeploymentPort,
  verifySelectedPorts
} from "./ports.mjs";

test("parsePort accepts valid integers", () => {
  assert.equal(parsePort("3000", "Web"), 3000);
  assert.equal(parsePort(8787, "API"), 8787);
});

test("parsePort rejects invalid values", () => {
  assert.throws(() => parsePort("0", "Web"), /Web/);
  assert.throws(() => parsePort("70000", "API"), /API/);
  assert.throws(() => parsePort("abc", "Web"), /Web/);
});

test("loops until the requested alternative is available", async () => {
  const answers = ["2", "3001", "3002"];
  const selected = await selectDeploymentPort({
    label: "Web",
    defaultPort: 3000,
    reserved: new Set([8787]),
    managedPorts: new Set(),
    ask: async () => answers.shift(),
    probe: async (port) => ({ available: port === 3002, owner: port === 3001 ? "node pid=42" : null })
  });
  assert.equal(selected, 3002);
});

test("accepts available default without choosing alternative", async () => {
  const selected = await selectDeploymentPort({
    label: "Web",
    defaultPort: 3000,
    reserved: new Set(),
    managedPorts: new Set(),
    ask: async () => "1",
    probe: async () => ({ available: true, owner: null }),
    print: () => {}
  });
  assert.equal(selected, 3000);
});

test("rejects duplicate reserved ports", async () => {
  await assert.rejects(
    selectDeploymentPort({
      label: "Web",
      defaultPort: 3000,
      reserved: new Set([3000]),
      managedPorts: new Set(),
      nonInteractive: true,
      ask: async () => assert.fail("must not prompt"),
      probe: async () => ({ available: true, owner: null })
    }),
    /already selected|reserved|冲突|duplicate/i
  );
});

test("non-interactive mode fails on an unknown listener", async () => {
  await assert.rejects(
    selectDeploymentPort({
      label: "API",
      defaultPort: 8787,
      reserved: new Set(),
      managedPorts: new Set(),
      nonInteractive: true,
      ask: async () => assert.fail("must not prompt"),
      probe: async () => ({ available: false, owner: "python pid=99" })
    }),
    /API port 8787 is already in use/
  );
});

test("quitting from occupied-port prompt exits safely", async () => {
  await assert.rejects(
    selectDeploymentPort({
      label: "Web",
      defaultPort: 3000,
      reserved: new Set(),
      managedPorts: new Set(),
      ask: async () => "q",
      probe: async () => ({ available: false, owner: "node pid=42" }),
      print: () => {}
    }),
    /cancelled|退出|abort/i
  );
});

test("managed ports may be selected during update preflight", async () => {
  const selected = await selectDeploymentPort({
    label: "Web",
    defaultPort: 3000,
    reserved: new Set(),
    managedPorts: new Set([3000]),
    nonInteractive: true,
    ask: async () => assert.fail("must not prompt"),
    probe: async () => ({ available: false, owner: "datafoundry pid=1" })
  });
  assert.equal(selected, 3000);
});

test("verifySelectedPorts fails when a port becomes occupied", async () => {
  await assert.rejects(
    verifySelectedPorts(
      [{ label: "Web", port: 3000 }],
      {
        managedPorts: new Set(),
        probe: async () => ({ available: false, owner: "nginx pid=7" })
      }
    ),
    /Web port 3000 is already in use/
  );
});

test("verifySelectedPorts skips ports still held by the managed stack", async () => {
  let probed = false;
  await verifySelectedPorts(
    [
      { label: "Web", port: 3000 },
      { label: "API", port: 8787 }
    ],
    {
      managedPorts: new Set([3000]),
      probe: async (port) => {
        if (port === 3000) {
          probed = true;
          return { available: false, owner: "datafoundry pid=1" };
        }
        return { available: true, owner: null };
      }
    }
  );
  assert.equal(probed, false);
});
