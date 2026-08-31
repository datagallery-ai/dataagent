/** Normalize probe failures into stable PROVIDER_* messages for the config API. */
export const modelProfileTestFailureMessage = (error: unknown): string => {
  const message = error instanceof Error ? error.message : String(error);
  const trimmed = message.trim() || "Model provider probe failed.";
  if (trimmed.startsWith("PROVIDER_CONFIG_MISSING:") || trimmed.startsWith("PROVIDER_TEST_FAILED:")) {
    return trimmed;
  }
  if (
    error instanceof Error
    && (error.name === "TimeoutError" || error.name === "AbortError" || /timed?\s*out|aborted/iu.test(trimmed))
  ) {
    return "PROVIDER_TEST_FAILED:Connection timed out while reaching the model provider.";
  }
  return `PROVIDER_TEST_FAILED:${trimmed}`;
};

/** Short success confirmation for connectivity test responses. */
export const modelProfileTestSuccessReason = (input: {
  model: string;
  response: string;
}): string => {
  const response = input.response.trim();
  if (response) {
    return `Model "${input.model}" responded successfully (${response}).`;
  }
  return `Model "${input.model}" responded successfully.`;
};

export const isRevisionConflictError = (error: unknown): boolean => {
  const message = error instanceof Error ? error.message : String(error);
  return message.startsWith("REVISION_CONFLICT:");
};
