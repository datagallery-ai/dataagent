import { existsSync, readdirSync, realpathSync } from "node:fs";
import path from "node:path";

export type PythonRuntimeConfig = {
  pythonBin: string;
  venvRoot: string;
};

/** Resolve the Python venv used by execute_command for data/ML scripts. */
export const resolvePythonRuntime = (): PythonRuntimeConfig | undefined => {
  const explicit = process.env.WORKSPACE_PYTHON_VENV?.trim();
  const candidates = [
    explicit,
    process.env.INIT_CWD ? path.resolve(process.env.INIT_CWD, ".venv") : undefined,
    path.resolve(process.cwd(), ".venv")
  ].filter((value): value is string => Boolean(value));

  const seen = new Set<string>();
  for (const candidate of candidates) {
    const venvRoot = path.resolve(candidate);
    if (seen.has(venvRoot)) {
      continue;
    }
    seen.add(venvRoot);

    const pythonBin = path.join(venvRoot, "bin", "python");
    if (!existsSync(pythonBin)) {
      continue;
    }

    return { venvRoot, pythonBin };
  }

  return undefined;
};

/** Locate `<venv>/lib/python3.x/site-packages` for PYTHONPATH injection. */
export const resolvePythonSitePackages = (venvRoot: string): string | undefined => {
  const libDir = path.join(venvRoot, "lib");
  if (!existsSync(libDir)) {
    return undefined;
  }

  for (const entry of readdirSync(libDir)) {
    if (!entry.startsWith("python3.")) {
      continue;
    }
    const sitePackages = path.join(libDir, entry, "site-packages");
    if (existsSync(sitePackages)) {
      return sitePackages;
    }
  }

  return undefined;
};

export const buildPythonSandboxEnv = (python: PythonRuntimeConfig): NodeJS.ProcessEnv => {
  const hostPath = process.env.PATH ?? "/usr/bin:/bin";
  const sitePackages = resolvePythonSitePackages(python.venvRoot);
  return {
    PATH: hostPath,
    ...(sitePackages ? { PYTHONPATH: sitePackages } : {}),
    MPLBACKEND: "Agg",
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONUNBUFFERED: "1",
    VIRTUAL_ENV: python.venvRoot
  };
};

/** Bubblewrap/seatbelt read allowlist for venv packages (interpreter runs from system python3.12). */
export const resolvePythonSandboxReadPaths = (python: PythonRuntimeConfig): string[] => {
  try {
    return [realpathSync(python.venvRoot)];
  } catch {
    return [python.venvRoot];
  }
};
