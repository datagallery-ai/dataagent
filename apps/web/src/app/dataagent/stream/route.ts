import { proxyToApi } from "../../../lib/api-proxy";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";
export const fetchCache = "force-no-store";

export function POST(request: Request): Promise<Response> {
  return proxyToApi(request, "/dataagent/stream");
}
