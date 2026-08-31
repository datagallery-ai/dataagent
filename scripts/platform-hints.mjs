/**
 * Cross-platform helpers for npm install / dev environment checks.
 */
import { existsSync } from "node:fs";
import { join } from "node:path";

export const MIN_NODE_MAJOR = 22;

/** Tailwind 4 / lightningcss optional natives that npm selects per OS+arch. */
const NATIVE_CSS_BY_PLATFORM = {
  win32: {
    x64: ["@tailwindcss/oxide-win32-x64-msvc", "lightningcss-win32-x64-msvc"],
    arm64: ["@tailwindcss/oxide-win32-arm64-msvc", "lightningcss-win32-arm64-msvc"],
  },
  linux: {
    x64: ["@tailwindcss/oxide-linux-x64-gnu", "lightningcss-linux-x64-gnu"],
    arm64: ["@tailwindcss/oxide-linux-arm64-gnu", "lightningcss-linux-arm64-gnu"],
  },
  darwin: {
    x64: ["@tailwindcss/oxide-darwin-x64", "lightningcss-darwin-x64"],
    arm64: ["@tailwindcss/oxide-darwin-arm64", "lightningcss-darwin-arm64"],
  },
};

const LINUX_MUSL_ALTERNATES = {
  x64: ["@tailwindcss/oxide-linux-x64-musl", "lightningcss-linux-x64-musl"],
  arm64: ["@tailwindcss/oxide-linux-arm64-musl", "lightningcss-linux-arm64-musl"],
};

const MIXED_INSTALL_MARKERS = {
  win32: ["@tailwindcss/oxide-win32-x64-msvc", "lightningcss-win32-x64-msvc"],
  linux: ["@tailwindcss/oxide-linux-x64-gnu", "lightningcss-linux-x64-gnu"],
};

export function localBinExists(root, name) {
  const binDir = join(root, "node_modules", ".bin");
  if (process.platform === "win32") {
    return existsSync(join(binDir, `${name}.cmd`)) || existsSync(join(binDir, name));
  }
  return existsSync(join(binDir, name));
}

export function packageInstalled(root, packageName) {
  const segments = packageName.split("/");
  const direct = join(root, "node_modules", ...segments);
  if (existsSync(direct)) {
    return true;
  }

  const nestedCandidates = [];
  if (packageName.startsWith("@tailwindcss/oxide-")) {
    nestedCandidates.push(
      join(root, "node_modules", "@tailwindcss", "oxide", "node_modules", ...segments),
    );
  }
  if (packageName.startsWith("lightningcss-") && packageName !== "lightningcss") {
    nestedCandidates.push(join(root, "node_modules", "lightningcss", "node_modules", packageName));
  }

  return nestedCandidates.some((candidate) => existsSync(candidate));
}

export function expectedNativeCssPackages(platform = process.platform, arch = process.arch) {
  const byArch = NATIVE_CSS_BY_PLATFORM[platform];
  if (!byArch) return [];
  return byArch[arch] ?? byArch.x64 ?? [];
}

function nativePackageSatisfied(root, packageName, platform, arch) {
  if (packageInstalled(root, packageName)) {
    return true;
  }
  if (platform !== "linux") {
    return false;
  }
  const alternates = LINUX_MUSL_ALTERNATES[arch] ?? LINUX_MUSL_ALTERNATES.x64 ?? [];
  const index = (NATIVE_CSS_BY_PLATFORM.linux[arch] ?? NATIVE_CSS_BY_PLATFORM.linux.x64 ?? []).indexOf(
    packageName,
  );
  if (index < 0) {
    return false;
  }
  const alternate = alternates[index];
  return alternate ? packageInstalled(root, alternate) : false;
}

/** @returns {string[]} missing package names for the current platform */
export function missingNativeCssPackages(root, platform = process.platform, arch = process.arch) {
  const expected = expectedNativeCssPackages(platform, arch);
  return expected.filter((pkg) => !nativePackageSatisfied(root, pkg, platform, arch));
}

/** Detect node_modules built for a different OS (common WSL ↔ Windows pitfall). */
export function detectMixedPlatformInstall(root, platform = process.platform) {
  const hasWin = MIXED_INSTALL_MARKERS.win32.some((pkg) => packageInstalled(root, pkg));
  const hasLinux = MIXED_INSTALL_MARKERS.linux.some((pkg) => packageInstalled(root, pkg));

  if (hasWin && hasLinux) {
    return (
      "Both Windows and Linux native CSS modules are present. " +
      "Remove node_modules and run npm install once on your current OS."
    );
  }
  if (platform === "win32" && hasLinux && !hasWin) {
    return (
      "Linux-native modules found while running on Windows. " +
      "Do not share node_modules with WSL — delete node_modules and run npm install on Windows."
    );
  }
  if (platform === "linux" && hasWin && !hasLinux) {
    return (
      "Windows-native modules found while running on Linux/WSL. " +
      "Do not run npm install from Windows against this tree — delete node_modules and run npm install on Linux."
    );
  }
  return null;
}

export function nodeVersionMessage(currentVersion) {
  return (
    `Node.js ${MIN_NODE_MAJOR}+ required (current: ${currentVersion}). ` +
    "Install Node 22+ for your platform (nvm, fnm, or nodejs.org)."
  );
}
