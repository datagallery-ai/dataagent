import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  configBaseUrlFromRuntime,
  fetchJsonWithStartupTimeout,
  preflightDefaultDatasourceId,
  preflightRuntimeConnection,
} from "./startup-preflight.js";

describe("startup preflight auth transport", () => {
  it("uses the injected fetch for healthz and run-defaults", async () => {
    const calls: string[] = [];
    const fetchImpl: typeof fetch = async (input) => {
      calls.push(String(input));
      if (String(input).endsWith("/healthz")) {
        return new Response("ok", { status: 200 });
      }
      return new Response(
        JSON.stringify({
          success: true,
          data: { activeDatasourceId: "ds-auth" },
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    };

    assert.equal(
      await preflightRuntimeConnection("http://127.0.0.1:8787/api/copilotkit", fetchImpl),
      true,
    );
    assert.equal(
      await preflightDefaultDatasourceId("http://127.0.0.1:8787", fetchImpl),
      "ds-auth",
    );
    assert.deepEqual(calls, [
      "http://127.0.0.1:8787/healthz",
      "http://127.0.0.1:8787/api/v1/run-defaults",
    ]);
  });

  it("keeps startup timeout armed through JSON body read", async () => {
    let abortedDuringBody = false;
    const result = await fetchJsonWithStartupTimeout(
      "http://127.0.0.1/api/v1/run-defaults",
      async (_input, init) =>
        new Response(
          new ReadableStream({
            start(controller) {
              const timer = setTimeout(() => {
                controller.enqueue(
                  new TextEncoder().encode(
                    JSON.stringify({
                      success: true,
                      data: { activeDatasourceId: "late" },
                    }),
                  ),
                );
                controller.close();
              }, 2_000);
              init?.signal?.addEventListener("abort", () => {
                abortedDuringBody = true;
                clearTimeout(timer);
                try {
                  controller.error(Object.assign(new Error("aborted"), { name: "AbortError" }));
                } catch {
                  // already closed
                }
              });
            },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
    );
    assert.equal(result, undefined);
    assert.equal(abortedDuringBody, true);
  });

  it("derives config base URL from runtime URL", () => {
    assert.equal(
      configBaseUrlFromRuntime("http://127.0.0.1:8787/api/copilotkit"),
      "http://127.0.0.1:8787",
    );
  });
});
