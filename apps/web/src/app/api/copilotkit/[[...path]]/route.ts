import { proxyToApi } from "../../../../lib/api-proxy";

// AG-UI / CopilotKit is an SSE stream. These segment configs are the App Router
// contract for a live passthrough — not optional caching knobs.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const fetchCache = "force-no-store";

type RouteContext = {
  params: Promise<{ path?: string[] }>;
};

async function handle(request: Request, context: RouteContext): Promise<Response> {
  const { path = [] } = await context.params;
  const suffix = path.length > 0 ? `/${path.join("/")}` : "";
  return proxyToApi(request, `/api/copilotkit${suffix}`);
}

export const GET = handle;
export const POST = handle;
export const PATCH = handle;
export const PUT = handle;
export const DELETE = handle;
export const OPTIONS = handle;
