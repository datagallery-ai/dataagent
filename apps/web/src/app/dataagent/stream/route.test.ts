import { afterEach, describe, expect, it, vi } from "vitest";
import { POST } from "./route";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("POST /dataagent/stream", () => {
  it("proxies the body and marks the response unbuffered", async () => {
    let target = "";
    let method = "";
    let forwarded = "";
    vi.stubGlobal("fetch", async (url: string, init: RequestInit) => {
      target = url;
      method = init.method ?? "";
      forwarded = new TextDecoder().decode(init.body as ArrayBuffer);
      return new Response('data: {"type":"RUN_STARTED"}\n\n', {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    });

    const response = await POST(
      new Request("http://localhost/dataagent/stream?x=1", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: '{"threadId":"thread-1"}',
      }),
    );

    expect(target).toBe("http://127.0.0.1:8790/dataagent/stream?x=1");
    expect(method).toBe("POST");
    expect(forwarded).toBe('{"threadId":"thread-1"}');
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(response.headers.get("X-Accel-Buffering")).toBe("no");
    expect(response.headers.get("Cache-Control")).toBe("no-cache, no-transform");
    expect(await response.text()).toContain("RUN_STARTED");
  });
});
