import assert from "node:assert/strict";
import dns from "node:dns/promises";
import { createServer } from "node:net";
import test from "node:test";
import {
  parsePort,
  readPortListeners,
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
    reserved: [{ port: 8787 }],
    ask: async () => answers.shift(),
    probe: async (port) => ({ available: port === 3002, owner: port === 3001 ? "node pid=42" : null })
  });
  assert.equal(selected, 3002);
});

test("accepts available default without choosing alternative", async () => {
  const selected = await selectDeploymentPort({
    label: "Web",
    defaultPort: 3000,
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
      reserved: [{ host: "127.0.0.1", port: 3000 }],
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
    nonInteractive: true,
    ask: async () => assert.fail("must not prompt"),
    probe: async () => ({ available: false, managed: true })
  });
  assert.equal(selected, 3000);
});

test("verifySelectedPorts fails when a port becomes occupied", async () => {
  await assert.rejects(
    verifySelectedPorts(
      [{ label: "Web", port: 3000 }],
      {
        retries: 0,
        probe: async () => ({ available: false, owner: "nginx pid=7" })
      }
    ),
    /Web port 3000 is already in use/
  );
});
test("different local addresses can select the same port", async t => {
  if (process.platform !== "linux") return t.skip("fixture uses Linux loopback aliases");
  const server = createServer();
  await new Promise(resolve => server.listen(0, "127.0.0.3", resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const port = server.address().port;
  const web = { label: "Web", host: "127.0.0.1", port };
  const api = { label: "API", host: "127.0.0.2", port };
  assert.equal(await selectDeploymentPort({
    ...web, defaultPort: port, reserved: [api], nonInteractive: true,
  }), port);
  await verifySelectedPorts([web, api], { retries: 0 });
});

test("Web localhost binds the first address while API localhost reserves both families", async t => {
  const addresses = [{ address: "::1", family: 6 }, { address: "127.0.0.1", family: 4 }];
  t.mock.method(dns, "lookup", async (hostname, options) => {
    assert.equal(hostname, "localhost");
    return options?.all ? addresses : addresses[0];
  });
  const probe = async () => ({ available: true });
  const web = { label: "Web", host: "localhost", port: 3000 };
  const api = { label: "API", host: "127.0.0.1", port: 3000, allAddresses: true };
  await verifySelectedPorts([web, api], { probe });
  await assert.rejects(
    verifySelectedPorts([{ ...web, host: "::1" }, { ...api, host: "localhost" }], { probe }),
    /already selected for another service/
  );
});

test("wildcard and specific addresses cannot select the same port on the same family", async () => {
  await assert.rejects(verifySelectedPorts([
    { label: "Web", host: "0.0.0.0", port: 3000 },
    { label: "API", host: "127.0.0.1", port: 3000 },
  ], { probe: async () => ({ available: true }) }), /already selected for another service/);
});

test("verifySelectedPorts retries briefly for transient TIME_WAIT occupancy", async () => {
  let attempts = 0;
  await verifySelectedPorts(
    [{ label: "Web", port: 3000 }],
    {
      retries: 3,
      retryDelayMs: 1,
      probe: async () => {
        attempts += 1;
        if (attempts < 3) return { available: false, owner: "unknown process" };
        return { available: true, owner: null };
      }
    }
  );
  assert.equal(attempts, 3);
});

test("readPortListeners selects the local port and includes every socket owner", async () => {
  const sockets = await readPortListeners(3000, {
    runSs: async () =>
      [
        "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process",
        'LISTEN 0 511 0.0.0.0:30001 0.0.0.0:* users:(("other",pid=9,fd=3))',
        'LISTEN 0 511 0.0.0.0:3000 0.0.0.0:* users:(("node",pid=42,fd=3),("worker",pid=43,fd=3))'
      ].join("\n")
  });
  assert.deepEqual(sockets, [{
    host: "0.0.0.0", port: 3000, ipv6Only: false,
    owners: [{ name: "node", pid: 42 }, { name: "worker", pid: 43 }],
  }]);
});
