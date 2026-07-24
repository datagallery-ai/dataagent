const STARTUP_PREFLIGHT_TIMEOUT_MS = 1200;

export async function fetchWithStartupTimeout(
  url: string,
  fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis),
): Promise<Response | undefined> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), STARTUP_PREFLIGHT_TIMEOUT_MS);

  try {
    return await fetchImpl(url, {
      method: "GET",
      signal: controller.signal,
    });
  } catch {
    return undefined;
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Like {@link fetchWithStartupTimeout}, but keeps the AbortController armed until
 * response body JSON parsing finishes (not only until headers arrive).
 */
export async function fetchJsonWithStartupTimeout(
  url: string,
  fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis),
): Promise<{ ok: boolean; body: unknown } | undefined> {
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), STARTUP_PREFLIGHT_TIMEOUT_MS);

  try {
    const response = await fetchImpl(url, {
      method: "GET",
      signal: controller.signal,
    });
    if (!response.ok) {
      return { ok: false, body: undefined };
    }
    const body = await response.json();
    return { ok: true, body };
  } catch {
    return undefined;
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function preflightRuntimeConnection(
  runtimeUrl: string,
  fetchImpl: typeof fetch,
): Promise<boolean> {
  const response = await fetchWithStartupTimeout(
    runtimeUrl.replace(/\/api\/.*$/, "/healthz"),
    fetchImpl,
  );
  return response?.ok === true;
}

function objectRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" ? value as Record<string, unknown> : undefined;
}

export function datasourceIdFromRunDefaults(value: unknown): string | undefined {
  const envelope = objectRecord(value);
  const data = objectRecord(envelope?.success === true ? envelope.data : value);
  const activeDatasourceId = data?.activeDatasourceId;

  if (typeof activeDatasourceId === "string" && activeDatasourceId.trim()) {
    return activeDatasourceId;
  }

  const enabledDatasourceIds = data?.enabledDatasourceIds;
  if (Array.isArray(enabledDatasourceIds)) {
    const firstEnabled = enabledDatasourceIds.find(
      (item): item is string => typeof item === "string" && item.trim().length > 0,
    );
    return firstEnabled;
  }

  return undefined;
}

export async function preflightDefaultDatasourceId(
  baseUrl: string,
  fetchImpl: typeof fetch,
): Promise<string | undefined> {
  const result = await fetchJsonWithStartupTimeout(
    `${baseUrl.replace(/\/$/, "")}/api/v1/run-defaults`,
    fetchImpl,
  );
  if (!result?.ok) {
    return undefined;
  }

  try {
    return datasourceIdFromRunDefaults(result.body);
  } catch {
    return undefined;
  }
}

export function configBaseUrlFromRuntime(runtimeUrl: string): string {
  const apiIndex = runtimeUrl.indexOf("/api/");
  if (apiIndex >= 0) {
    return runtimeUrl.slice(0, apiIndex);
  }
  return runtimeUrl.replace(/\/$/, "");
}
