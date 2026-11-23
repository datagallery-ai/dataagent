import { describe, expect, it, vi } from "vitest";
import { invokeWithReportedError } from "../invoke-with-reported-error";

describe("invokeWithReportedError", () => {
  it("returns the action result when nothing throws", () => {
    const reportError = vi.fn();
    expect(invokeWithReportedError(() => 42, reportError)).toBe(42);
    expect(reportError).not.toHaveBeenCalled();
  });

  it("reports synchronous errors instead of letting them escape", () => {
    const reportError = vi.fn();
    const error = new Error("crypto.randomUUID is not a function");

    expect(
      invokeWithReportedError(() => {
        throw error;
      }, reportError),
    ).toBeUndefined();

    expect(reportError).toHaveBeenCalledWith(error);
  });
});
