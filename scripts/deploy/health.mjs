import net from "node:net";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function probeHttp(url, options = {}) {
  const timeoutMs = options.timeoutMs ?? 5000;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { signal: controller.signal, redirect: "manual" });
    return { ok: response.status >= 200 && response.status < 400, status: response.status };
  } catch (error) {
    return { ok: false, status: null, error: error?.message ?? String(error) };
  } finally {
    clearTimeout(timer);
  }
}

export async function probeTcp(host, port, options = {}) {
  const timeoutMs = options.timeoutMs ?? 5000;
  return new Promise((resolve) => {
    const socket = net.connect({ host, port });
    const timer = setTimeout(() => {
      socket.destroy();
      resolve({ ok: false, error: "timeout" });
    }, timeoutMs);
    socket.once("connect", () => {
      clearTimeout(timer);
      socket.end();
      resolve({ ok: true });
    });
    socket.once("error", (error) => {
      clearTimeout(timer);
      resolve({ ok: false, error: error.message });
    });
  });
}

async function resolveProcessAlive(config) {
  if (typeof config.checkProcessAlive === "function") {
    return Boolean(await config.checkProcessAlive());
  }
  return Boolean(config.processAlive);
}

export async function collectDeploymentHealth(config, options = {}) {
  const probeTimeoutMs = options.probeTimeoutMs ?? 5000;
  const processAlive = await resolveProcessAlive(config);
  const processStatus = processAlive ? "running" : "stopped";

  const health = await probeHttp(`${config.apiBaseUrl}/healthz`, { timeoutMs: probeTimeoutMs });
  const ready = await probeHttp(`${config.apiBaseUrl}/ready`, { timeoutMs: probeTimeoutMs });
  const web = await probeHttp(config.webUrl, { timeoutMs: probeTimeoutMs });

  const apiHealth = health.ok && health.status === 200 ? "healthy" : "unhealthy";
  const apiReady = ready.ok && ready.status === 200 ? "ready" : "unhealthy";
  const webStatus = web.ok ? "reachable" : "unreachable";
  const ok =
    processStatus === "running" &&
    apiHealth === "healthy" &&
    apiReady === "ready" &&
    webStatus === "reachable";

  return {
    process: processStatus,
    apiHealth,
    apiReady,
    web: webStatus,
    ok
  };
}

export async function waitForDeployment(config, options = {}) {
  const intervalMs = options.intervalMs ?? 1000;
  const timeoutMs = options.timeoutMs ?? 60_000;
  const deadline = Date.now() + timeoutMs;
  let summary;

  while (Date.now() <= deadline) {
    summary = await collectDeploymentHealth(config, options);
    if (summary.ok) return summary;
    await sleep(intervalMs);
  }

  const error = new Error(
    `Deployment health check timed out: process=${summary.process} apiHealth=${summary.apiHealth} apiReady=${summary.apiReady} web=${summary.web}`
  );
  error.summary = summary;
  throw error;
}
