import { spawn } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

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

function parseNodeVersion(text) {
  const match = /v?(\d+)\.(\d+)\.(\d+)/.exec(text ?? "");
  if (!match) return null;
  return { major: Number(match[1]), minor: Number(match[2]), patch: Number(match[3]), raw: match[0] };
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
  const nodeVersion = parseNodeVersion(node.stdout || node.stderr);
  entries.push({
    name: "node",
    required: true,
    foundVersion: nodeVersion?.raw ?? null,
    minimumVersion: "22",
    status: nodeVersion && nodeVersion.major >= 22 ? "ok" : "missing",
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

  for (const entry of missing) {
    const action = entry.installAction;
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
