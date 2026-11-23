/** Shared outcome rules for streamed results and restored conversation history. */
export function toolResultPayloadLooksFailed(value: unknown): boolean {
  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return false;
    }
  }
  const payload = recordValue(value);
  if (!payload) return false;
  return hasFailure(payload) || hasFailure(recordValue(payload.result));
}

function hasFailure(payload: Record<string, unknown> | null): boolean {
  if (!payload) return false;
  if (payload.status === "error" || payload.status === "failed" || payload.isError === true) {
    return true;
  }
  // Successful tool payloads may include error: null, false or an empty string.
  return typeof payload.error === "string" ? payload.error.trim().length > 0 : Boolean(payload.error);
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}
