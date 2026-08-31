/**
 * Process-lifetime memoization for idempotent async bootstrap work.
 * Coalesces in-flight calls for the same key so concurrent first requests
 * share one execution.
 */
export function createAsyncMemoByKey<TArgs extends unknown[], TResult>(
  run: (...args: TArgs) => Promise<TResult>,
  keyOf: (...args: TArgs) => string,
): (...args: TArgs) => Promise<TResult> {
  const results = new Map<string, TResult>();
  const inflight = new Map<string, Promise<TResult>>();

  return async (...args: TArgs): Promise<TResult> => {
    const key = keyOf(...args);
    if (results.has(key)) {
      return results.get(key) as TResult;
    }
    const existing = inflight.get(key);
    if (existing) {
      return existing;
    }
    const pending = (async () => {
      try {
        const result = await run(...args);
        results.set(key, result);
        return result;
      } finally {
        inflight.delete(key);
      }
    })();
    inflight.set(key, pending);
    return pending;
  };
}

export type StartupPhaseTimings = Record<string, number>;

/** Wall-clock phase timer for createServer cold-start observability. */
export function createStartupTimer(): {
  measure: <T>(name: string, work: () => Promise<T> | T) => Promise<T>;
  timings: () => StartupPhaseTimings;
  totalMs: () => number;
} {
  const timings: StartupPhaseTimings = {};
  const startedAt = performance.now();

  return {
    async measure<T>(name: string, work: () => Promise<T> | T): Promise<T> {
      const t0 = performance.now();
      try {
        return await work();
      } finally {
        timings[name] = Math.round(performance.now() - t0);
      }
    },
    timings: () => ({ ...timings }),
    totalMs: () => Math.round(performance.now() - startedAt),
  };
}
