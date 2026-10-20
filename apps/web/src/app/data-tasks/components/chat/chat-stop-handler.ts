export type ChatRunCancellationOptions = {
  fallbackTimeoutMs?: number;
  onCancelRun?: () => Promise<void> | void;
  onStopFrontend?: () => void;
  throwOnCancelFailure?: boolean;
};

export function performChatRunCancellation(
  options: ChatRunCancellationOptions,
): Promise<void> {
  const onCancelRun = options.onCancelRun;
  if (!onCancelRun) {
    options.onStopFrontend?.();
    return Promise.resolve();
  }

  return new Promise((resolve, reject) => {
    let settled = false;
    const fallback = () => {
      if (settled) return;
      settled = true;
      options.onStopFrontend?.();
      resolve();
    };
    const timeout = setTimeout(fallback, options.fallbackTimeoutMs ?? 5000);

    const finish = (error?: unknown) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      options.onStopFrontend?.();
      if (error !== undefined && options.throwOnCancelFailure) {
        reject(error);
        return;
      }
      resolve();
    };
    try {
      void Promise.resolve(onCancelRun()).then(
        () => finish(),
        (error) => finish(error),
      );
    } catch (error) {
      finish(error);
    }
  });
}

export function createChatStopHandler(
  options: ChatRunCancellationOptions,
): () => void {
  return () => {
    void performChatRunCancellation(options);
  };
}
