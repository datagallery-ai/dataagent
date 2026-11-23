import { afterEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "./route";

afterEach(() => {
  vi.unstubAllGlobals();
});

function context(...path: string[]) {
  return { params: Promise.resolve({ path }) };
}

describe("/api/v1 proxy", () => {
  it("streams POST /api/v1/runs unbuffered", async () => {
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
      new Request("http://localhost/api/v1/runs?x=1", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: '{"threadId":"thread-1"}',
      }),
      context("runs"),
    );

    expect(target).toBe("http://127.0.0.1:8790/api/v1/runs?x=1");
    expect(method).toBe("POST");
    expect(forwarded).toBe('{"threadId":"thread-1"}');
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(response.headers.get("X-Accel-Buffering")).toBe("no");
    expect(response.headers.get("Cache-Control")).toBe("no-cache, no-transform");
    expect(await response.text()).toContain("RUN_STARTED");
  });

  it("forwards session reads with the query string", async () => {
    let target = "";
    vi.stubGlobal("fetch", async (url: string) => {
      target = url;
      return Response.json({ success: true, data: { sessions: [] } });
    });
    const response = await GET(new Request("http://localhost/api/v1/sessions?limit=5"), context("sessions"));
    expect(target).toBe("http://127.0.0.1:8790/api/v1/sessions?limit=5");
    expect(await response.json()).toEqual({ success: true, data: { sessions: [] } });
  });
});
