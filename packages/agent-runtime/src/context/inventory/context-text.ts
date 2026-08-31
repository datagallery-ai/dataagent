export const truncateContextText = (value: string, maxChars: number): string =>
  truncateString(value, maxChars)?.value ?? value;

export const truncateString = (value: string, maxChars: number): { truncated: boolean; value: string } | undefined => {
  if (value.length <= maxChars) {
    return { truncated: false, value };
  }

  return {
    truncated: true,
    value: `${value.slice(0, maxChars)} [truncated, original ${value.length} chars]`
  };
};
