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
    let fallbackUsed = false;
    const fallback = () => {
      if (settled || fallbackUsed) {
        return;
      }
      fallbackUsed = true;
      options.onStopFrontend?.();
      resolve();
    };
    const timeout = setTimeout(fallback, options.fallbackTimeoutMs ?? 5000);

    try {
      void Promise.resolve(onCancelRun()).then(
        () => {
          settled = true;
          clearTimeout(timeout);
          resolve();
        },
        (error) => {
          clearTimeout(timeout);
          if (options.throwOnCancelFailure) {
            settled = true;
            reject(error);
            return;
          }
          fallback();
        },
      );
    } catch (error) {
      clearTimeout(timeout);
      if (options.throwOnCancelFailure) {
        settled = true;
        reject(error);
        return;
      }
      fallback();
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
