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
  // /ready is the conversation engine. A deploy without LLM_* still brings the
  // process up; status keeps apiReady so that case is not reported as ready.
  const ok =
    processStatus === "running" &&
    apiHealth === "healthy" &&
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
    const processAlive = await resolveProcessAlive(config);
    if (!processAlive) throw new Error("Managed process exited before deployment became healthy");
    summary = await collectDeploymentHealth({ ...config, checkProcessAlive: undefined, processAlive }, options);
    if (summary.ok) return summary;
    await sleep(intervalMs);
  }

  const error = new Error(
    `Deployment health check timed out: process=${summary.process} apiHealth=${summary.apiHealth} apiReady=${summary.apiReady} web=${summary.web}`
  );
  error.summary = summary;
  throw error;
}
