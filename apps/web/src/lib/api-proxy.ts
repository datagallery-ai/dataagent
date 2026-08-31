const DEFAULT_API_TARGET = "http://127.0.0.1:8787";

/** Headers that tell intermediaries (nginx, CDNs, Next compress) not to buffer SSE. */
export const STREAMING_PROXY_HEADERS = {
  "Cache-Control": "no-cache, no-transform",
  "X-Accel-Buffering": "no",
} as const;

function readProxyTarget(...values: Array<string | undefined>): string {
  for (const value of values) {
    const trimmed = value?.trim();
    if (trimmed) {
      return trimmed.replace(/\/$/u, "");
    }
  }
  return DEFAULT_API_TARGET;
}

export function getApiProxyTarget(): string {
  return readProxyTarget(
    process.env.API_PROXY_TARGET,
    process.env.NEXT_PUBLIC_CONFIG_API_URL,
    process.env.NEXT_PUBLIC_AGENT_RUNTIME_URL?.replace(/\/api\/copilotkit\/?$/u, ""),
  );
}

export function isStreamingProxyPath(pathname: string): boolean {
  return pathname === "/api/copilotkit" || pathname.startsWith("/api/copilotkit/");
}

export function isStreamingContentType(contentType: string | null): boolean {
  return Boolean(contentType?.toLowerCase().includes("text/event-stream"));
}

/**
 * Apply hop-by-hop cleanup and SSE anti-buffering headers on a proxied response.
 * Keeps `upstream.body` as a ReadableStream — never buffer the response body.
 */
export function buildProxyResponseHeaders(
  upstreamHeaders: Headers,
  pathname: string,
): Headers {
  const responseHeaders = new Headers(upstreamHeaders);
  // Recompute length/encoding for this hop; forwarding upstream values breaks streaming.
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("content-length");
  responseHeaders.delete("transfer-encoding");

  if (
    isStreamingProxyPath(pathname) ||
    isStreamingContentType(responseHeaders.get("content-type"))
  ) {
    for (const [key, value] of Object.entries(STREAMING_PROXY_HEADERS)) {
      responseHeaders.set(key, value);
    }
  }

  return responseHeaders;
}

export async function proxyToApi(request: Request, pathname: string): Promise<Response> {
  const incoming = new URL(request.url);
  const targetUrl = `${getApiProxyTarget()}${pathname}${incoming.search}`;
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("connection");
  headers.delete("content-length");

  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
    // Do not cache proxied API traffic (especially AG-UI event streams).
    cache: "no-store",
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    // Agent run bodies are small JSON; buffering the request is fine.
    // Response body must remain a stream (see buildProxyResponseHeaders).
    init.body = await request.arrayBuffer();
  }

  const upstream = await fetch(targetUrl, init);
  const responseHeaders = buildProxyResponseHeaders(upstream.headers, pathname);

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}
