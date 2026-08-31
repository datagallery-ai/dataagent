import { describe, expect, it } from "vitest";
import {
  STREAMING_PROXY_HEADERS,
  buildProxyResponseHeaders,
  isStreamingContentType,
  isStreamingProxyPath,
} from "../api-proxy";

describe("api-proxy streaming contract", () => {
  it("recognizes CopilotKit paths as streaming", () => {
    expect(isStreamingProxyPath("/api/copilotkit")).toBe(true);
    expect(isStreamingProxyPath("/api/copilotkit/info")).toBe(true);
    expect(isStreamingProxyPath("/api/v1/sessions")).toBe(false);
  });

  it("detects text/event-stream content types", () => {
    expect(isStreamingContentType("text/event-stream")).toBe(true);
    expect(isStreamingContentType("text/event-stream; charset=utf-8")).toBe(true);
    expect(isStreamingContentType("application/json")).toBe(false);
    expect(isStreamingContentType(null)).toBe(false);
  });

  it("strips hop-by-hop length/encoding and sets anti-buffering headers for SSE", () => {
    const upstream = new Headers({
      "content-type": "text/event-stream; charset=utf-8",
      "content-encoding": "gzip",
      "content-length": "999",
      "transfer-encoding": "chunked",
      "x-request-id": "abc",
    });

    const headers = buildProxyResponseHeaders(upstream, "/api/v1/other");

    expect(headers.get("content-type")).toBe("text/event-stream; charset=utf-8");
    expect(headers.get("x-request-id")).toBe("abc");
    expect(headers.get("content-encoding")).toBeNull();
    expect(headers.get("content-length")).toBeNull();
    expect(headers.get("transfer-encoding")).toBeNull();
    expect(headers.get("Cache-Control")).toBe(STREAMING_PROXY_HEADERS["Cache-Control"]);
    expect(headers.get("X-Accel-Buffering")).toBe(STREAMING_PROXY_HEADERS["X-Accel-Buffering"]);
  });

  it("applies anti-buffering headers for CopilotKit even without event-stream yet", () => {
    const headers = buildProxyResponseHeaders(new Headers({ "content-type": "application/json" }), "/api/copilotkit");

    expect(headers.get("Cache-Control")).toBe(STREAMING_PROXY_HEADERS["Cache-Control"]);
    expect(headers.get("X-Accel-Buffering")).toBe("no");
  });

  it("does not force streaming headers on ordinary JSON API responses", () => {
    const headers = buildProxyResponseHeaders(
      new Headers({ "content-type": "application/json", "content-length": "12" }),
      "/api/v1/sessions",
    );

    expect(headers.get("Cache-Control")).toBeNull();
    expect(headers.get("X-Accel-Buffering")).toBeNull();
    expect(headers.get("content-length")).toBeNull();
  });
});
