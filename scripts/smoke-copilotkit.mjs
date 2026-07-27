import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const apiPort = process.env.API_PORT ?? "8798";
const apiBaseUrl = `http://127.0.0.1:${apiPort}`;
const metadataDbPath = process.env.METADATA_DB_PATH ?? `storage/metadata/copilotkit-smoke-${Date.now()}.sqlite`;

const child = spawn("npm", ["--workspace", "@datafoundry/api", "run", "dev"], {
  env: {
    ...process.env,
    API_HOST: "127.0.0.1",
    API_PORT: apiPort,
    METADATA_DB_PATH: metadataDbPath,
    DATAFOUNDRY_AUTH_MODE: "password",
    AUTH_SESSION_SECRET: process.env.AUTH_SESSION_SECRET ?? "copilotkit-smoke-session-secret-32b!",
    AUTH_PUBLIC_BASE_URL: process.env.AUTH_PUBLIC_BASE_URL ?? "http://127.0.0.1:3000",
    AUTH_EMAIL_DELIVERY: process.env.AUTH_EMAIL_DELIVERY ?? "test",
    AUTH_REGISTRATION_MODE: process.env.AUTH_REGISTRATION_MODE ?? "open"
  },
  stdio: ["ignore", "pipe", "pipe"]
});

let output = "";
child.stdout.on("data", (chunk) => {
  output += chunk.toString();
});
child.stderr.on("data", (chunk) => {
  output += chunk.toString();
});

try {
  await waitForHealth(apiBaseUrl);
  const client = createAuthenticatedTestClient({ baseUrl: apiBaseUrl });
  await client.registerAndLogin({ displayName: "CopilotKit Smoke" });

  const optionsResponse = await client.fetch("/api/copilotkit", {
    method: "OPTIONS",
    headers: {
      Origin: "http://127.0.0.1:3000",
      "Access-Control-Request-Method": "POST"
    }
  });

  if (optionsResponse.status !== 204) {
    throw new Error(`Unexpected CopilotKit OPTIONS status: ${optionsResponse.status}`);
  }

  const postResponse = await client.fetch("/api/copilotkit", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({})
  });
  const body = await postResponse.json();

  const validMissingProviderResponse =
    postResponse.status === 503 && body?.error?.code === "PROVIDER_CONFIG_MISSING";
  const validRuntimeValidationResponse = postResponse.status === 400 && body?.message === "Missing method field";

  if (!validMissingProviderResponse && !validRuntimeValidationResponse) {
    throw new Error(`Unexpected CopilotKit validation response: ${postResponse.status} ${JSON.stringify(body)}`);
  }

  console.log("CopilotKit smoke OK: /api/copilotkit CORS and runtime validation are reachable");
} finally {
  child.kill("SIGTERM");
}

async function waitForHealth(baseUrl) {
  const startedAt = Date.now();

  while (Date.now() - startedAt < 15000) {
    try {
      const response = await fetch(`${baseUrl}/healthz`);

      if (response.ok) {
        return;
      }
    } catch {
      // Retry until the dev server is ready.
    }

    await delay(300);
  }

  throw new Error(`API did not become healthy. Output:\n${output}`);
}
