import { proxyToApi } from "../../lib/api-proxy";

// Same path as the control plane: GET /sessions.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const fetchCache = "force-no-store";

export async function GET(request: Request): Promise<Response> {
  return proxyToApi(request, "/sessions");
}
