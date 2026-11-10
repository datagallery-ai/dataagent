import { proxyToApi } from "../../../../lib/api-proxy";

// Same path as the control plane: POST /runs/{runId}/cancel.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const fetchCache = "force-no-store";

type RouteContext = {
  params: Promise<{ runId: string }>;
};

export async function POST(request: Request, context: RouteContext): Promise<Response> {
  const { runId } = await context.params;
  return proxyToApi(request, `/runs/${encodeURIComponent(runId)}/cancel`);
}
