import { proxyToApi } from "../../../../lib/api-proxy";

// Same path as the control plane: GET /sessions/{threadId}/outputs.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const fetchCache = "force-no-store";

type RouteContext = {
  params: Promise<{ threadId: string }>;
};

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  const { threadId } = await context.params;
  return proxyToApi(request, `/sessions/${encodeURIComponent(threadId)}/outputs`);
}
