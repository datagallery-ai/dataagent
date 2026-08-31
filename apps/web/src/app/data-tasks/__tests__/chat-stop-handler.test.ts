import { describe, expect, it, vi } from "vitest";
import {
  createChatStopHandler,
  performChatRunCancellation,
} from "../components/chat/chat-stop-handler";

describe("performChatRunCancellation", () => {
  it("resolves after backend cancellation succeeds", async () => {
    const onStopFrontend = vi.fn();
    const onCancelRun = vi.fn().mockResolvedValue(undefined);

    await performChatRunCancellation({ onCancelRun, onStopFrontend });

    expect(onCancelRun).toHaveBeenCalledTimes(1);
    expect(onStopFrontend).not.toHaveBeenCalled();
  });

  it("falls back to frontend stop when backend cancellation rejects", async () => {
    const onStopFrontend = vi.fn();
    const onCancelRun = vi.fn().mockRejectedValue(new Error("cancel failed"));

    await performChatRunCancellation({ onCancelRun, onStopFrontend });

    expect(onCancelRun).toHaveBeenCalledTimes(1);
    expect(onStopFrontend).toHaveBeenCalledTimes(1);
  });

  it("rejects without frontend fallback when strict cancellation rejects", async () => {
    const onStopFrontend = vi.fn();
    const onCancelRun = vi.fn().mockRejectedValue(new Error("cancel failed"));

    await expect(
      performChatRunCancellation({
        onCancelRun,
        onStopFrontend,
        throwOnCancelFailure: true,
      }),
    ).rejects.toThrow("cancel failed");

    expect(onCancelRun).toHaveBeenCalledTimes(1);
    expect(onStopFrontend).not.toHaveBeenCalled();
  });

  it("falls back to frontend stop when backend cancellation times out", async () => {
    vi.useFakeTimers();
    const onStopFrontend = vi.fn();
    const onCancelRun = vi.fn(() => new Promise<void>(() => undefined));
    const cancelled = performChatRunCancellation({
      onCancelRun,
      onStopFrontend,
      fallbackTimeoutMs: 25,
    });

    await vi.advanceTimersByTimeAsync(24);
    expect(onStopFrontend).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1);
    await cancelled;
    expect(onStopFrontend).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});

describe("createChatStopHandler", () => {
  it("requests backend cancellation without stopping the frontend stream when cancellation succeeds", async () => {
    const onStopFrontend = vi.fn();
    const onCancelRun = vi.fn().mockResolvedValue(undefined);
    const stop = createChatStopHandler({ onCancelRun, onStopFrontend });

    stop();
    await Promise.resolve();

    expect(onCancelRun).toHaveBeenCalledTimes(1);
    expect(onStopFrontend).not.toHaveBeenCalled();
  });

  it("still stops the frontend stream when backend cancel is unavailable", () => {
    const onStopFrontend = vi.fn();
    const stop = createChatStopHandler({ onStopFrontend });

    stop();

    expect(onStopFrontend).toHaveBeenCalledTimes(1);
  });

  it("falls back to frontend stop when backend cancellation fails", async () => {
    const onStopFrontend = vi.fn();
    const onCancelRun = vi.fn().mockRejectedValue(new Error("cancel failed"));
    const stop = createChatStopHandler({ onCancelRun, onStopFrontend });

    stop();
    await Promise.resolve();
    await Promise.resolve();

    expect(onCancelRun).toHaveBeenCalledTimes(1);
    expect(onStopFrontend).toHaveBeenCalledTimes(1);
  });

  it("falls back to frontend stop when backend cancellation times out", async () => {
    vi.useFakeTimers();
    const onStopFrontend = vi.fn();
    const onCancelRun = vi.fn(() => new Promise<void>(() => undefined));
    const stop = createChatStopHandler({
      onCancelRun,
      onStopFrontend,
      fallbackTimeoutMs: 25,
    });

    stop();
    await vi.advanceTimersByTimeAsync(24);
    expect(onStopFrontend).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1);
    expect(onStopFrontend).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("requests backend cancellation when frontend stop is unavailable", async () => {
    const onCancelRun = vi.fn().mockResolvedValue(undefined);
    const stop = createChatStopHandler({ onCancelRun });

    stop();
    await Promise.resolve();

    expect(onCancelRun).toHaveBeenCalledTimes(1);
  });
});
