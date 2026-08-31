import { describe, expect, it } from "vitest";
import { createTranslator } from "../../../i18n/translate";
import { ConfigApiError } from "../../../lib/config-api";
import {
  formatConfigTestError,
  formatConfigTestResult,
} from "../config-test-result";

describe("config test result presentation", () => {
  const t = createTranslator("en");

  it("formats LLM connection details for inline display", () => {
    expect(
      formatConfigTestResult(
        "llm",
        {
          model: "qwen-plus",
          latencyMs: 1200,
          response: "OK",
          status: "connected",
        },
        t,
      ),
    ).toEqual({
      tone: "success",
      title: "Test succeeded",
      details: ["Model: qwen-plus", "Duration: 1200 ms", "Response: OK"],
    });
  });

  it("prefers the backend reason on success", () => {
    expect(
      formatConfigTestResult(
        "llm",
        {
          model: "qwen-plus",
          latencyMs: 1200,
          response: "OK",
          reason: 'Model "qwen-plus" responded successfully (OK).',
          status: "connected",
        },
        t,
      ),
    ).toEqual({
      tone: "success",
      title: "Test succeeded",
      details: [
        'Model "qwen-plus" responded successfully (OK).',
        "Duration: 1200 ms",
      ],
    });
  });

  it("formats connection test failures for inline display", () => {
    expect(
      formatConfigTestError(
        new Error("PROVIDER_TEST_FAILED:Incorrect API key provided"),
        t,
      ),
    ).toEqual({
      tone: "error",
      title: "Test failed",
      details: ["Incorrect API key provided"],
    });
  });

  it("humanizes provider config missing and revision conflicts", () => {
    expect(
      formatConfigTestError(
        new ConfigApiError(
          "PROVIDER_TEST_FAILED",
          "PROVIDER_CONFIG_MISSING:custom-1",
          422,
        ),
        t,
      ).details[0],
    ).toContain("API key");

    expect(
      formatConfigTestError(
        new ConfigApiError("REVISION_CONFLICT", "REVISION_CONFLICT:custom-1", 409),
        t,
      ).details[0],
    ).toContain("updated while testing");

    expect(
      formatConfigTestError(
        new ConfigApiError("REVISION_CONFLICT", "custom-1", 409),
        t,
      ).details[0],
    ).toContain("updated while testing");
  });

  it("humanizes datasource, MCP, and deleted-while-testing errors", () => {
    expect(
      formatConfigTestError(
        new ConfigApiError(
          "DATASOURCE_TEST_FAILED",
          "DATASOURCE_TEST_FAILED:connection refused",
          422,
        ),
        t,
      ).details[0],
    ).toBe("connection refused");

    expect(
      formatConfigTestError(
        new ConfigApiError("BAD_REQUEST", "MCP_TEST_FAILED:ECONNREFUSED", 422),
        t,
      ).details[0],
    ).toBe("ECONNREFUSED");

    expect(
      formatConfigTestError(
        new ConfigApiError("BAD_REQUEST", "MCP_SERVER_CONFIG_INVALID", 400),
        t,
      ).details[0],
    ).toContain("MCP server configuration");

    expect(
      formatConfigTestError(
        new ConfigApiError("RESOURCE_NOT_FOUND", "CONFIG_RESOURCE_NOT_FOUND:kb-1", 404),
        t,
      ).details[0],
    ).toContain("deleted while testing");
  });

  it("formats skipped probes for kb/skill without claiming connected", () => {
    expect(
      formatConfigTestResult(
        "kb",
        {
          status: "untested",
          tested: false,
          reason: "Connectivity probe is not available for this resource type.",
        },
        t,
      ),
    ).toEqual({
      tone: "success",
      title: "Test skipped",
      details: ["Connectivity probe is not available for this resource type."],
    });
  });
});
