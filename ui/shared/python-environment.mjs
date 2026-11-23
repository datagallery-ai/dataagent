import path from "node:path";

export function venvPython(project, env = process.env, platform = process.platform) {
  const environment = path.resolve(project, env.UV_PROJECT_ENVIRONMENT || ".venv");
  return path.join(environment, platform === "win32" ? "Scripts/python.exe" : "bin/python");
}
