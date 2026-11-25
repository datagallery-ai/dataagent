import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { isSupportedNodeVersion, MIN_NODE_VERSION } from "../platform-hints.mjs";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "../..");

async function defaultRun(command, args = [], options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd ?? ROOT,
      env: options.env ?? process.env,
      stdio: ["ignore", "pipe", "pipe"]
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve({ stdout, stderr, code });
      else {
        const error = new Error(stderr.trim() || `${command} exited ${code}`);
        error.code = code;
        error.stdout = stdout;
        error.stderr = stderr;
        reject(error);
      }
    });
  });
}

async function safeRun(run, command, args = []) {
  try {
    return await run(command, args);
  } catch (error) {
    return { error, stdout: error?.stdout ?? "", stderr: error?.stderr ?? "" };
  }
}

export async function inspectDependencies(options = {}) {
  const run = options.run ?? defaultRun;
  const entries = [];

  const node = await safeRun(run, "node", ["--version"]);
  const nodeVersion = (node.stdout || node.stderr).trim();
  entries.push({
    name: "node",
    required: true,
    foundVersion: nodeVersion || null,
    minimumVersion: MIN_NODE_VERSION,
    status: !node.error && isSupportedNodeVersion(nodeVersion) ? "ok" : "missing",
    installAction: "node"
  });

  const npm = await safeRun(run, "npm", ["--version"]);
  const npmVersion = (npm.stdout || "").trim() || null;
  entries.push({
    name: "npm",
    required: true,
    foundVersion: npmVersion,
    minimumVersion: "any",
    status: npmVersion ? "ok" : "missing",
    installAction: "node"
  });

  if (options.includeUv === false) return entries;
  const uv = await safeRun(run, "uv", ["--version"]);
  const uvVersion = /uv\s+([0-9][^\s]*)/i.exec(`${uv.stdout || ""}\n${uv.stderr || ""}`)?.[1]
    ?? ((uv.stdout || "").trim() || null);
  entries.push({
    name: "uv",
    required: true,
    foundVersion: uv.error ? null : uvVersion,
    minimumVersion: "any",
    status: uv.error ? "missing" : "ok",
    installAction: "uv"
  });

  return entries;
}

async function canInstallNonInteractive(options, run) {
  if ((options.uid ?? process.getuid?.() ?? 1000) === 0) return true;
  const result = await safeRun(run, "sudo", ["-n", "true"]);
  return !result.error;
}

export async function ensureDependencies(options = {}) {
  const run = options.run ?? defaultRun;
  const ask = options.ask;
  const install = options.install ?? (async (action, installOptions = {}) => {
    const args = [path.join(ROOT, "scripts/deploy/install-dependency.sh"), action];
    if (installOptions.nonInteractive) {
      args.push("--non-interactive");
    }
    await run("bash", args);
  });
  const print = options.print ?? ((message) => process.stdout.write(`${message}\n`));

  let entries = await inspectDependencies(options);
  const missing = entries.filter((entry) => entry.status !== "ok");
  if (missing.length === 0) return entries;

  if (missing.some(entry => entry.name === "uv")) {
    throw new Error(
      "缺少 uv。请先将 uv 安装到 PATH 后重试；自定义下载源配置方法见 docs/zh/guides/mirror-deployment.md；官方安装说明：https://docs.astral.sh/uv/getting-started/installation/"
    );
  }
  const actions = new Set();
  for (const entry of missing) {
    const action = entry.installAction;
    if (actions.has(action)) continue;
    actions.add(action);
    const commandHint = `bash scripts/deploy/install-dependency.sh ${action}`;
    if (options.nonInteractive) {
      const allowed = await canInstallNonInteractive(options, run);
      if (!allowed) {
        throw new Error(
          `Missing ${entry.name}. Non-interactive install requires root or passwordless sudo. Or run: ${commandHint}`
        );
      }
      print(`Installing ${entry.name} via ${commandHint}`);
      await install(action, { nonInteractive: true });
      continue;
    }

    print(`缺少依赖：${entry.name}（需要 ${entry.minimumVersion}，当前 ${entry.foundVersion ?? "未找到"}）`);
    print(`将执行：${commandHint}`);
    const answer = String(await ask("是否安装？[y/N]：" )).trim().toLowerCase();
    if (answer !== "y" && answer !== "yes") {
      throw new Error(`Dependency ${entry.name} is required. Install with: ${commandHint}`);
    }
    await install(action, { nonInteractive: false });
  }

  entries = await inspectDependencies(options);
  const stillMissing = entries.filter((entry) => entry.status !== "ok");
  if (stillMissing.length > 0) {
    throw new Error(
      `Dependencies still missing after install: ${stillMissing.map((e) => e.name).join(", ")}`
    );
  }
  return entries;
}
