/** Run a sync action and surface thrown errors to the UI reporter instead of failing silently. */
export function invokeWithReportedError<T>(
  action: () => T,
  reportError: (error: unknown) => void,
): T | undefined {
  try {
    return action();
  } catch (error) {
    reportError(error);
    return undefined;
  }
}
