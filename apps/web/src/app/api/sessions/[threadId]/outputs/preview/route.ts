import { proxyToApi } from "../../../../../../lib/api-proxy";

// Text preview of one session output file (`?path=` is forwarded as-is).
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const fetchCache = "force-no-store";

type RouteContext = {
  params: Promise<{ threadId: string }>;
};

export async function GET(request: Request, context: RouteContext): Promise<Response> {
  const { threadId } = await context.params;
  return proxyToApi(request, `/sessions/${encodeURIComponent(threadId)}/outputs/preview`);
}
