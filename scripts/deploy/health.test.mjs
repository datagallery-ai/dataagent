import assert from "node:assert/strict";
import http from "node:http";
import net from "node:net";
import test from "node:test";
import {
  collectDeploymentHealth,
  probeHttp,
  probeTcp,
  waitForDeployment
} from "./health.mjs";

function listen(server) {
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });
}

test("probeHttp and probeTcp succeed against ephemeral servers", async () => {
  const server = http.createServer((_req, res) => {
    res.writeHead(200);
    res.end("ok");
  });
  const port = await listen(server);
  try {
    const result = await probeHttp(`http://127.0.0.1:${port}/`);
    assert.equal(result.ok, true);
    assert.equal(result.status, 200);
    const tcp = await probeTcp("127.0.0.1", port);
    assert.equal(tcp.ok, true);
  } finally {
    server.close();
  }
});

test("collectDeploymentHealth reports healthy stack", async () => {
  const api = http.createServer((req, res) => {
    if (req.url === "/healthz" || req.url === "/ready") {
      res.writeHead(200);
      res.end("ok");
      return;
    }
    res.writeHead(404);
    res.end();
  });
  const web = http.createServer((_req, res) => {
    res.writeHead(302);
    res.end();
  });
  const apiPort = await listen(api);
  const webPort = await listen(web);
  try {
    const summary = await collectDeploymentHealth({
      processAlive: true,
      apiBaseUrl: `http://127.0.0.1:${apiPort}`,
      webUrl: `http://127.0.0.1:${webPort}`
    });
    assert.deepEqual(summary, {
      process: "running",
      apiHealth: "healthy",
      apiReady: "ready",
      web: "reachable",
      ok: true
    });
  } finally {
    api.close();
    web.close();
  }
});

test("waitForDeployment retries until ready then times out with summary", async () => {
  let ready = false;
  const api = http.createServer((req, res) => {
    if (req.url === "/healthz") {
      res.writeHead(200);
      res.end("ok");
      return;
    }
    if (req.url === "/ready") {
      res.writeHead(ready ? 200 : 503);
      res.end(ready ? "ok" : "starting");
      return;
    }
    res.writeHead(404);
    res.end();
  });
  const web = http.createServer((_req, res) => {
    res.writeHead(200);
    res.end("web");
  });
  const apiPort = await listen(api);
  const webPort = await listen(web);
  const config = {
    processAlive: true,
    apiBaseUrl: `http://127.0.0.1:${apiPort}`,
    webUrl: `http://127.0.0.1:${webPort}`
  };

  try {
    setTimeout(() => {
      ready = true;
    }, 80);
    const summary = await waitForDeployment(config, {
      intervalMs: 40,
      timeoutMs: 1000,
      probeTimeoutMs: 200
    });
    assert.equal(summary.ok, true);

    await assert.rejects(
      () => waitForDeployment({ ...config, processAlive: false }, {
        intervalMs: 20,
        timeoutMs: 60,
        probeTimeoutMs: 50
      }),
      /process|unhealthy|timeout/i
    );
  } finally {
    api.close();
    web.close();
  }
});

test("waitForDeployment rejects an exited process immediately", async () => {
  let aliveChecks = 0;
  // Keep HTTP healthy so the only failing gate is processAlive, avoiding a race
  // where /ready flips true while the callback still reports alive.
  const api = http.createServer((req, res) => {
    if (req.url === "/healthz" || req.url === "/ready") {
      res.writeHead(200);
      res.end("ok");
      return;
    }
    res.writeHead(404);
    res.end("missing");
  });
  const web = http.createServer((_req, res) => {
    res.writeHead(200);
    res.end("web");
  });
  const apiPort = await listen(api);
  const webPort = await listen(web);

  try {
    await assert.rejects(
      () =>
        waitForDeployment(
          {
            checkProcessAlive: async () => {
              aliveChecks += 1;
              return false;
            },
            apiBaseUrl: `http://127.0.0.1:${apiPort}`,
            webUrl: `http://127.0.0.1:${webPort}`
          },
          { intervalMs: 20, timeoutMs: 120, probeTimeoutMs: 50 }
        ),
      /process exited/i
    );
    assert.equal(aliveChecks, 1);
  } finally {
    api.close();
    web.close();
  }
});

test("collectDeploymentHealth keeps the stack up when the conversation engine is not ready", async () => {
  const api = http.createServer((req, res) => {
    if (req.url === "/healthz") {
      res.writeHead(200);
      res.end("ok");
      return;
    }
    if (req.url === "/ready") {
      res.writeHead(503);
      res.end("not ready");
      return;
    }
    res.writeHead(404);
    res.end();
  });
  const web = http.createServer((_req, res) => {
    res.writeHead(200);
    res.end("web");
  });
  const apiPort = await listen(api);
  const webPort = await listen(web);
  try {
    const summary = await collectDeploymentHealth({
      processAlive: true,
      apiBaseUrl: `http://127.0.0.1:${apiPort}`,
      webUrl: `http://127.0.0.1:${webPort}`
    });
    assert.equal(summary.apiHealth, "healthy");
    assert.equal(summary.apiReady, "unhealthy");
    assert.equal(summary.ok, true);
  } finally {
    api.close();
    web.close();
  }
});

test("TCP probe fails for closed ports", async () => {
  const server = net.createServer();
  const port = await new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });
  await new Promise((resolve) => server.close(resolve));
  const result = await probeTcp("127.0.0.1", port, { timeoutMs: 100 });
  assert.equal(result.ok, false);
});

test("health waiting stops probing as soon as the managed process exits", async t => {
  let checks = 0;
  let requests = 0;
  t.mock.method(globalThis, "fetch", async () => {
    requests++;
    return new Response(null, { status: 503 });
  });
  await assert.rejects(waitForDeployment({
    checkProcessAlive: async () => ++checks === 1,
    apiBaseUrl: "http://api.example", webUrl: "http://web.example",
  }, { intervalMs: 1, timeoutMs: 100 }), /process exited/i);
  assert.equal(checks, 2);
  assert.equal(requests, 3);
});
