import { createHttpRuntimeClient } from "./client.js";
import { createInProcessRuntime } from "./in-process.js";
import {
  RUNTIME_CONTRACT_VERSION,
  RUNTIME_PROVIDER,
  type RuntimeHealth,
  type RuntimeTransport
} from "./types.js";

export type RuntimeFactoryOptions = {
  token?: string;
  url?: string;
};

export const resolveRuntimeServiceUrl = (
  env: Record<string, string | undefined> = process.env
): string | undefined => {
  const value = env.RUNTIME_SERVICE_URL?.trim();
  return value ? value.replace(/\/+$/, "") : undefined;
};

export const createRuntimeTransport = (
  options: RuntimeFactoryOptions = {}
): RuntimeTransport => {
  const url = options.url ?? resolveRuntimeServiceUrl();
  if (!url) {
    return createInProcessRuntime();
  }
  return createHttpRuntimeClient({
    url,
    ...(options.token ? { token: options.token } : {})
  });
};

export const unavailableRuntimeHealth = (): RuntimeHealth => ({
  status: "unavailable",
  provider: RUNTIME_PROVIDER,
  version: RUNTIME_CONTRACT_VERSION,
  capabilities: {
    streaming: false,
    tools: false,
    interrupt: false,
    cancel: false
  }
});

export const probeRuntimeHealth = async (transport: RuntimeTransport): Promise<RuntimeHealth> => {
  try {
    return await transport.health();
  } catch {
    return unavailableRuntimeHealth();
  }
};
