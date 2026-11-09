import { proxyToApi } from "../../../../../lib/api-proxy";

// Session output files are served by the conversation server outside the /api/v1 product API.
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
