import assert from "node:assert/strict";
import test from "node:test";

import {
  llmEnvFingerprint,
  modelProfileConnectivityPayloadChanged,
  preferConnectedResourceId,
  resolveModelProfileSaveStatus,
  serverDefaultConnectionStatus,
  isServerLlmEnvConfigured
} from "../apps/api/src/model-profile-connection-status.ts";

test("resolveModelProfileSaveStatus keeps connected until connectivity changes", () => {
  assert.equal(
    resolveModelProfileSaveStatus({
      isNew: false,
      currentStatus: "connected",
      credentialsUpdated: false,
      connectivityChanged: false
    }),
    "connected"
  );
  assert.equal(
    resolveModelProfileSaveStatus({
      isNew: false,
      currentStatus: "connected",
      credentialsUpdated: true,
      connectivityChanged: false
    }),
    "untested"
  );
  assert.equal(
    resolveModelProfileSaveStatus({
      isNew: false,
      currentStatus: "connected",
      credentialsUpdated: false,
      connectivityChanged: true
    }),
    "untested"
  );
});

test("modelProfileConnectivityPayloadChanged detects provider and model updates", () => {
  assert.equal(
    modelProfileConnectivityPayloadChanged(
      { provider: "openai-compatible", modelName: "qwen-plus", baseUrl: "https://example/v1" },
      { provider: "openai-compatible", modelName: "qwen-max", baseUrl: "https://example/v1" }
    ),
    true
  );
  assert.equal(
    modelProfileConnectivityPayloadChanged(
      { provider: "openai-compatible", modelName: "qwen-plus", baseUrl: "https://example/v1" },
      { provider: "openai-compatible", modelName: "qwen-plus", baseUrl: "https://example/v1" }
    ),
    false
  );
});

test("serverDefaultConnectionStatus invalidates stale connected status", () => {
  const env = {
    LLM_PROVIDER: "openai-compatible",
    LLM_BASE_URL: "https://example/v1",
    LLM_MODEL: "qwen-plus",
    LLM_API_KEY: "secret"
  };
  const fingerprint = llmEnvFingerprint(env);
  assert.equal(
    serverDefaultConnectionStatus({
      currentStatus: "connected",
      storedFingerprint: fingerprint,
      env
    }),
    "connected"
  );
  assert.equal(
    serverDefaultConnectionStatus({
      currentStatus: "connected",
      storedFingerprint: fingerprint,
      env: { ...env, LLM_MODEL: "qwen-max" }
    }),
    "untested"
  );
  assert.equal(
    serverDefaultConnectionStatus({
      currentStatus: "connected",
      storedFingerprint: undefined,
      env
    }),
    "untested"
  );
});

test("isServerLlmEnvConfigured requires API key, base URL, and model", () => {
  assert.equal(
    isServerLlmEnvConfigured({
      LLM_API_KEY: "secret",
      LLM_BASE_URL: "https://example/v1",
      LLM_MODEL: "qwen-plus"
    }),
    true
  );
  assert.equal(
    isServerLlmEnvConfigured({
      LLM_API_KEY: "secret",
      LLM_BASE_URL: "https://example/v1"
    }),
    false
  );
  assert.equal(
    isServerLlmEnvConfigured({
      LLM_BASE_URL: "https://example/v1",
      LLM_MODEL: "qwen-plus"
    }),
    false
  );
  assert.equal(
    isServerLlmEnvConfigured({
      LLM_API_KEY: "secret",
      LLM_MODEL: "qwen-plus"
    }),
    false
  );
  assert.equal(isServerLlmEnvConfigured({}), false);
});

test("preferConnectedResourceId picks connected over earlier failed profiles", () => {
  assert.equal(
    preferConnectedResourceId([
      { id: "glm-failed", status: "failed" },
      { id: "deepseek-ok", status: "connected" },
      { id: "qwen-failed", status: "failed" }
    ]),
    "deepseek-ok"
  );
  assert.equal(
    preferConnectedResourceId([
      { id: "glm-failed", status: "failed" },
      { id: "qwen-untested", status: "untested" }
    ]),
    "glm-failed"
  );
  assert.equal(preferConnectedResourceId([]), undefined);
});
