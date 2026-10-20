import { describe, expect, it } from "vitest";

import { formatRunErrorMessage, isSessionRunBusyMessage } from "../run-error-message";

describe("formatRunErrorMessage", () => {
  it("explains RUN_TIMEOUT with seconds and guidance", () => {
    expect(formatRunErrorMessage("RUN_TIMEOUT:60000")).toContain("60s");
    expect(formatRunErrorMessage("RUN_TIMEOUT:60000")).toContain("Timeout (ms)");
  });

  it("explains missing model provider configuration", () => {
    expect(formatRunErrorMessage("PROVIDER_CONFIG_MISSING:bad-model")).toBe(
      'Model provider configuration is missing for "bad-model". Check the model profile API key, base URL, and model name.',
    );
  });

  it("passes through unknown messages", () => {
    expect(formatRunErrorMessage("Something broke")).toBe("Something broke");
  });

  it("explains missing crypto.randomUUID on insecure HTTP", () => {
    expect(
      formatRunErrorMessage("crypto.randomUUID is not a function"),
    ).toContain("secure context");
    expect(
      formatRunErrorMessage("Secure context required"),
    ).toContain("HTTPS");
  });

  it("falls back when message is empty", () => {
    expect(formatRunErrorMessage()).toBe("Agent run failed");
  });

  it("explains a busy session in the active locale", () => {
    const zh = (key: string) => (key === "chatInput.sessionBusyElsewhere" ? "该会话正在其他窗口运行，请稍后或新建会话" : key);
    const en = (key: string) => (key === "chatInput.sessionBusyElsewhere" ? "another window" : key);
    expect(isSessionRunBusyMessage("A run is already active for this session")).toBe(true);
    expect(isSessionRunBusyMessage("HTTP 409 Conflict")).toBe(true);
    expect(isSessionRunBusyMessage("provider failed")).toBe(false);
    expect(formatRunErrorMessage("RUN_ALREADY_ACTIVE:run-1", zh)).toContain("其他窗口");
    expect(formatRunErrorMessage("A run is already active for this session", en)).toContain("another window");
  });
});
