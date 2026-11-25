export const MIN_NODE_VERSION = "22.12.0";

export function isSupportedNodeVersion(version) {
  const match = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(version.trim());
  if (!match) return false;
  const [major, minor, patch] = match.slice(1).map(Number);
  const [minimumMajor, minimumMinor, minimumPatch] = MIN_NODE_VERSION.split(".").map(Number);
  return major > minimumMajor || (major === minimumMajor &&
    (minor > minimumMinor || (minor === minimumMinor && patch >= minimumPatch)));
}
