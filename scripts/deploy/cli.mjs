import { spawn } from "node:child_process";
import { constants, existsSync } from "node:fs";
import { access, appendFile, copyFile, mkdir, readFile, statfs, writeFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { deploymentHelp, parseDeployArgs } from "./args.mjs";
import {
  ensureDeploymentEnvironment,
  isCompleteDeploymentConfig,
  parseDeploymentEnvironment,
  redactSensitiveText,
  renderWebEnvironment,
  updateDeploymentEnvironment,
  writeDeploymentConfiguration
} from "./config.mjs";
import { createDeploymentController } from "./controller.mjs";
import { ensureDependencies, inspectDependencies } from "./dependencies.mjs";
import { collectDeploymentHealth, probeHttp, waitForDeployment } from "./health.mjs";
import { probePort, selectDeploymentPort, verifySelectedPorts } from "./ports.mjs";
import {
  deploymentPaths,
  isProcessAlive,
  readDeploymentState,
  startManagedStack,
  stopManagedStack,
  verifyManagedProcessForStop,
  writeDeploymentState
} from "./process-state.mjs";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "../..");
const TUI_ENTRY = "apps/tui/dist/index.js";

export function resolveTuiRuntimeUrl(env = {}, runtimeUrl = null) {
  const explicit = String(runtimeUrl ?? "").trim();
  if (explicit) return explicit;
  const port = Number(env.API_PORT || 8787);
  return `http://127.0.0.1:${port}/api/copilotkit`;
}

export async function inspectWritablePath(targetPath) {
  let candidate = path.resolve(targetPath);
  while (true) {
    try {
      await access(candidate, constants.W_OK);
      return { writable: true, checkedPath: candidate, exists: existsSync(targetPath) };
    } catch (error) {
      const parent = path.dirname(candidate);
      if (parent === candidate) {
        return {
          writable: false,
          checkedPath: candidate,
          exists: existsSync(targetPath),
          error: error?.message ?? String(error)
        };
      }
      candidate = parent;
    }
  }
}

function overlayProcessEnv(text, processEnv = {}) {
  const updates = {};
  for (const key of [
    "WEB_HOST",
    "WEB_PORT",
    "API_HOST",
    "API_PORT",
    "AUTH_PUBLIC_BASE_URL",
    "AUTH_SESSION_SECRET",
    "SECRET_MASTER_KEY"
  ]) {
    if (processEnv[key] != null && String(processEnv[key]).trim() !== "") {
      updates[key] = String(processEnv[key]);
    }
  }
  return Object.keys(updates).length > 0 ? updateDeploymentEnvironment(text, updates) : text;
}

export function collectManagedPorts(env = {}, state = null) {
  const ports = new Set();
  for (const key of ["WEB_PORT", "API_PORT"]) {
    const port = Number(env[key]);
    if (Number.isInteger(port) && port > 0) ports.add(port);
  }
  if (state?.ports && typeof state.ports === "object") {
    for (const value of Object.values(state.ports)) {
      const port = Number(value);
      if (Number.isInteger(port) && port > 0) ports.add(port);
    }
  }
  return ports;
}

export function resolveAuthPublicBaseUrl(existing, oldWebPort, newWebPort) {
  const url = String(existing ?? "").trim();
  if (!url) return `http://127.0.0.1:${newWebPort}`;

  try {
    const parsed = new URL(url);
    const isLoopback = /^(127\.0\.0\.1|localhost)$/i.test(parsed.hostname);
    const explicitPort = parsed.port;

    if (isLoopback && url.includes(`:${oldWebPort}`)) {
      return url.replace(`:${oldWebPort}`, `:${newWebPort}`);
    }
    if (!explicitPort && !isLoopback) {
      return url;
    }
    if (isLoopback) {
      return `http://127.0.0.1:${newWebPort}`;
    }
    return url;
  } catch {
    return `http://127.0.0.1:${newWebPort}`;
  }
}

function formatDeployLogTimestamp(date = new Date()) {
  return date.toISOString().replace(/[-:TZ.]/g, "").slice(0, 14);
}

export async function createDeployLogWriter(root, options = {}) {
  const logsDir = path.join(root, "storage/logs");
  await mkdir(logsDir, { recursive: true });
  const stamp = options.timestamp ?? formatDeployLogTimestamp();
  const logFile = path.join(logsDir, `deploy-${stamp}.log`);
  const latestFile = path.join(logsDir, "deploy-latest.log");
  const relativeLogPath = path.join("storage/logs", `deploy-${stamp}.log`);

  await writeFile(logFile, `[${new Date().toISOString()}] deploy started\n`, {
    encoding: "utf8",
    mode: 0o600
  });

  let pendingLine = "";

  async function flushBufferedLines(text, { finalize = false } = {}) {
    pendingLine += String(text ?? "");
    let ready = "";

    while (true) {
      const newlineIndex = pendingLine.indexOf("\n");
      if (newlineIndex === -1) break;
      ready += pendingLine.slice(0, newlineIndex + 1);
      pendingLine = pendingLine.slice(newlineIndex + 1);
    }

    if (finalize && pendingLine.length > 0) {
      ready += pendingLine;
      pendingLine = "";
    }

    if (ready.length > 0) {
      await appendFile(logFile, redactSensitiveText(ready), { encoding: "utf8", mode: 0o600 });
    }
  }

  return {
    logPath: relativeLogPath,
    async append(text) {
      await flushBufferedLines(text);
    },
    async finalize() {
      await flushBufferedLines("", { finalize: true });
      await copyFile(logFile, latestFile);
    }
  };
}

const MAX_PORT_MENU_INVALID_CHOICES = 5;

async function selectPortWithHint(options) {
  return selectDeploymentPort({
    ...options,
    ask: async (prompt) => {
      if (!prompt.includes("请选择")) {
        return options.ask(prompt);
      }

      for (let attempt = 0; attempt < MAX_PORT_MENU_INVALID_CHOICES; attempt += 1) {
        const answer = await options.ask(prompt);
        if (!answer || ["1", "2", ""].includes(String(answer).trim())) {
          return answer;
        }
        options.print("请输入 1 或 2");
      }

      throw new Error("port selection cancelled: too many invalid choices");
    }
  });
}

export async function configureDeploymentInteractively(options) {
  const {
    root,
    sourceText,
    reconfigure,
    nonInteractive,
    ask,
    print,
    probe,
    processEnv = {},
    write = false,
    timestamp
  } = options;

  let text = overlayProcessEnv(sourceText ?? "", processEnv);
  const originalEnv = parseDeploymentEnvironment(text);
  const completeBeforeFill = isCompleteDeploymentConfig(originalEnv);
  let ensured = ensureDeploymentEnvironment(text);
  text = ensured.text;
  let env = ensured.env;
  const oldWebPort = env.WEB_PORT;

  if (!reconfigure && !nonInteractive && completeBeforeFill && sourceText?.trim()) {
    return { env, envText: text, webText: renderWebEnvironment(env), wrote: false };
  }

  const deploymentState = root ? await readDeploymentState(root).catch(() => null) : null;
  const managedPorts = collectManagedPorts(env, deploymentState);

  const reserved = new Set();
  const webPort = await selectPortWithHint({
    label: "Web",
    defaultPort: Number(env.WEB_PORT || 3000),
    reserved,
    managedPorts,
    nonInteractive,
    ask,
    print,
    probe
  });
  reserved.add(webPort);
  const apiPort = await selectPortWithHint({
    label: "API",
    defaultPort: Number(env.API_PORT || 8787),
    reserved,
    managedPorts,
    nonInteractive,
    ask,
    print,
    probe
  });
  reserved.add(apiPort);

  const updates = {
    WEB_PORT: String(webPort),
    API_PORT: String(apiPort),
    AUTH_PUBLIC_BASE_URL: resolveAuthPublicBaseUrl(env.AUTH_PUBLIC_BASE_URL, oldWebPort, webPort)
  };

  if (!nonInteractive) {
    const defaultUrl = updates.AUTH_PUBLIC_BASE_URL;
    const answer = String((await ask(`浏览器公开访问地址 [${defaultUrl}]：`)) ?? "").trim();
    if (answer) {
      let url;
      try {
        url = new URL(answer);
      } catch {
        throw new Error("AUTH_PUBLIC_BASE_URL must be a valid URL");
      }
      const urlPort = url.port || (url.protocol === "https:" ? "443" : "80");
      if (String(urlPort) !== String(webPort)) {
        const confirm = String(
          (await ask(`公开地址端口 ${urlPort} 与 Web 端口 ${webPort} 不一致。确认为反向代理？[y/N]：`)) ?? ""
        )
          .trim()
          .toLowerCase();
        if (confirm !== "y" && confirm !== "yes") {
          throw new Error("Public URL port must match the selected Web port");
        }
      }
      updates.AUTH_PUBLIC_BASE_URL = answer;
    }
  }

  text = updateDeploymentEnvironment(text, updates);
  ensured = ensureDeploymentEnvironment(text);
  text = ensured.text;
  env = ensured.env;

  if (
    (env.WEB_HOST === "0.0.0.0" || env.WEB_HOST === "::") &&
    /^https?:\/\/(127\.0\.0\.1|localhost)(:|\/|$)/i.test(env.AUTH_PUBLIC_BASE_URL)
  ) {
    print("警告：WEB_HOST 对外绑定，但 AUTH_PUBLIC_BASE_URL 仅适合本机访问。远程访问请设置正确的公开地址。");
  }

  const webText = renderWebEnvironment(env);
  let wrote = false;
  if (write) {
    await writeDeploymentConfiguration(root, text, webText, {
      backup: Boolean(reconfigure),
      timestamp
    });
    wrote = true;
  }

  return { env, envText: text, webText, wrote };
}

function createReadlineAsk() {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  const ask = (prompt) =>
    new Promise((resolve) => {
      rl.question(prompt, (answer) => resolve(answer));
    });
  ask.close = () => rl.close();
  return ask;
}

async function loadRootEnvText(root) {
  try {
    return await readFile(path.join(root, ".env"), "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") return "";
    throw error;
  }
}

function runCommand(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd ?? ROOT,
      env: options.env ?? process.env,
      stdio: options.log ? ["ignore", "pipe", "pipe"] : (options.stdio ?? "inherit")
    });

    if (options.log) {
      const tee = (stream, target) => {
        stream.on("data", (chunk) => {
          target.write(chunk);
          options.log.append(chunk).catch(() => {});
        });
      };
      tee(child.stdout, process.stdout);
      tee(child.stderr, process.stderr);
    }

    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve();
      else {
        const error = new Error(`${command} ${args.join(" ")} failed with code ${code}`);
        if (options.logPath) error.logPath = options.logPath;
        reject(error);
      }
    });
  });
}

function createRealDeps(context) {
  const { root, parsed, ask, print } = context;
  let config = null;
  let deployLog = null;

  const commandOptions = () => ({
    cwd: root,
    log: deployLog ?? undefined,
    logPath: deployLog?.logPath
  });

  return {
    async beginDeployLog() {
      deployLog = await createDeployLogWriter(root);
      return deployLog.logPath;
    },
    async finalizeDeployLog() {
      if (deployLog) await deployLog.finalize();
    },
    async loadConfiguration() {
      const sourceText = await loadRootEnvText(root);
      const overlaid = overlayProcessEnv(sourceText, process.env);
      const generateSecrets = parsed.command === "deploy";
      const ensured = ensureDeploymentEnvironment(overlaid, { generateSecrets });
      config = { env: ensured.env, envText: ensured.text, sourceText, generatedKeys: ensured.generatedKeys };
      return config;
    },
    async preflight(current, options) {
      if (options?.command === "start") {
        const apiDist = path.join(root, "apps/api/dist/index.js");
        const webBuild = path.join(root, "apps/web/.next/BUILD_ID");
        const webEnvLocal = path.join(root, "apps/web/.env.local");
        if (!existsSync(apiDist) || !existsSync(webBuild) || !existsSync(webEnvLocal)) {
          throw new Error("请先运行 ./deploy.sh deploy");
        }
        const diskEnv = parseDeploymentEnvironment(current.sourceText ?? "");
        if (!isCompleteDeploymentConfig(diskEnv)) {
          throw new Error("缺少合法完整的 .env 配置，请先运行 ./deploy.sh deploy");
        }
      }
    },
    async configure(current) {
      const configured = await configureDeploymentInteractively({
        root,
        sourceText: current.sourceText ?? "",
        reconfigure: parsed.reconfigure,
        nonInteractive: parsed.nonInteractive,
        ask,
        print,
        processEnv: process.env,
        write: false
      });
      config = { ...current, ...configured };
      return config;
    },
    async checkDependencies() {
      await ensureDependencies({
        nonInteractive: parsed.nonInteractive,
        ask,
        print
      });
    },
    async selectPorts(current) {
      return current;
    },
    async writeConfiguration(current) {
      await writeDeploymentConfiguration(root, current.envText, current.webText, {
        backup: parsed.reconfigure
      });
    },
    async isRunning() {
      const state = await readDeploymentState(root);
      return Boolean(state?.pid && isProcessAlive(state.pid));
    },
    async stop() {
      await stopManagedStack(root);
    },
    async installProject() {
      await runCommand("npm", ["ci"], commandOptions());
    },
    async buildTypeScript() {
      await runCommand("npm", ["run", "build"], commandOptions());
    },
    async buildWeb() {
      await runCommand("npm", ["run", "build:web"], commandOptions());
    },
    async buildTui() {
      await runCommand("npm", ["run", "build:tui"], commandOptions());
      const entry = path.join(root, TUI_ENTRY);
      if (!existsSync(entry)) {
        throw new Error(`TUI build did not produce ${TUI_ENTRY}`);
      }
    },
    async ensureTuiReady() {
      const entry = path.join(root, TUI_ENTRY);
      if (!existsSync(entry)) {
        throw new Error(`TUI is not built (${TUI_ENTRY}). Run ./deploy.sh deploy first.`);
      }
    },
    async checkApiForTui(current) {
      const apiBaseUrl = `http://127.0.0.1:${current.env.API_PORT}`;
      const health = await probeHttp(`${apiBaseUrl}/healthz`);
      if (!(health.ok && health.status === 200)) {
        throw new Error(
          `API is not healthy at ${apiBaseUrl}/healthz. Start the stack with ./deploy.sh start (or deploy), then retry ./deploy.sh tui.`
        );
      }
    },
    async startTui(current, parsed) {
      const runtimeUrl = resolveTuiRuntimeUrl(current.env, parsed?.runtimeUrl);
      print(`Starting TUI against ${runtimeUrl} (foreground client; Ctrl+C exits TUI only).`);
      await runCommand("npm", ["run", "start:tui", "--", "--runtime-url", runtimeUrl], {
        cwd: root,
        stdio: "inherit"
      });
    },

    async verifyPorts(current) {
      const services = [
        { label: "Web", port: current.env.WEB_PORT },
        { label: "API", port: current.env.API_PORT }
      ];
      // After stop-old, managed ports may still be briefly held by our previous process.
      // Treat them as owned so verify-ports-again does not false-fail during redeploy.
      const state = await readDeploymentState(root).catch(() => null);
      const managedPorts = collectManagedPorts(current.env, state);
      await verifySelectedPorts(services, { managedPorts });
    },
    async start(current) {
      await startManagedStack(root, {
        env: { ...process.env, ...current.env },
        ports: {
          web: Number(current.env.WEB_PORT),
          api: Number(current.env.API_PORT)
        }
      });
    },
    async waitForHealth(current) {
      try {
        return await waitForDeployment({
          checkProcessAlive: async () => {
            const state = await readDeploymentState(root);
            return Boolean(state?.pid && isProcessAlive(state.pid));
          },
          apiBaseUrl: `http://127.0.0.1:${current.env.API_PORT}`,
          webUrl: `http://127.0.0.1:${current.env.WEB_PORT}`
        });
      } catch (error) {
        const latest = await readDeploymentState(root);
        if (latest?.pid) {
          await writeDeploymentState(root, { ...latest, status: "unhealthy" });
        }
        throw error;
      }
    },
    async markHealthy(current) {
      const state = await readDeploymentState(root);
      if (!state?.pid || !isProcessAlive(state.pid)) {
        throw new Error("Managed process is not alive; refusing to mark deployment healthy");
      }
      if (process.platform === "linux") {
        const verification = await verifyManagedProcessForStop(state.pid, state.launchId);
        if (!verification.allowed) {
          throw new Error(
            `Refusing to mark healthy: launch marker does not identify DataFoundry (${verification.reason})`
          );
        }
      }
      await writeDeploymentState(root, { ...state, status: "healthy" });
      if (deployLog) await deployLog.finalize();
      print(`DataFoundry is healthy at ${current.env.AUTH_PUBLIC_BASE_URL}`);
      print("Next: open Web, register/login, then create and enable a model profile.");
      print("TUI is built and ready. Start it in another terminal (foreground client, not a background service):");
      print("  ./deploy.sh tui");
      print("  # or: npm run start:tui");
    },

    async status() {
      const state = await readDeploymentState(root);
      const env =
        config?.env ??
        ensureDeploymentEnvironment(await loadRootEnvText(root), { generateSecrets: false }).env;
      const summary = await collectDeploymentHealth({
        checkProcessAlive: async () => Boolean(state?.pid && isProcessAlive(state.pid)),
        apiBaseUrl: `http://127.0.0.1:${env.API_PORT}`,
        webUrl: `http://127.0.0.1:${env.WEB_PORT}`
      });
      print(`进程        ${summary.process}`);
      print(`API         ${summary.apiHealth}/${summary.apiReady}`);
      print(`Web         ${summary.web}`);
      return summary;
    },
    async logs() {
      const logPath = deploymentPaths(root).runtimeLog;
      await mkdir(path.dirname(logPath), { recursive: true });
      if (!existsSync(logPath)) await runCommand("touch", [logPath]);
      await runCommand("tail", ["-n", "200", "-F", logPath]);
    },
    async doctor() {
      await runDeploymentDoctor(root, { print });
    },
    async printHelp(text) {
      print(text);
    },
    async reportFailure(failure) {
      if (deployLog) await deployLog.finalize().catch(() => {});
      for (const line of formatDeploymentFailure(failure).split("\n")) {
        print(line);
      }
    }
  };
}

export async function runDeploymentDoctor(root, options = {}) {
  const print = options.print ?? (() => {});
  const lines = [];
  const record = (line) => {
    lines.push(line);
    print(line);
  };

  record(`os: ${process.platform} ${os.release()}`);
  record(`arch: ${os.arch()}`);

  const envText = await loadRootEnvText(root).catch(() => "");
  const ensured = ensureDeploymentEnvironment(envText, { generateSecrets: false });
  const env = ensured.env;

  const deps = await inspectDependencies({ run: options.run });
  for (const entry of deps) {
    record(
      `dependency ${entry.name}: ${entry.status}${entry.foundVersion ? ` (${entry.foundVersion})` : ""}`
    );
  }

  const configIssues = [];
  if (!env.WEB_PORT) configIssues.push("WEB_PORT missing");
  if (!env.API_PORT) configIssues.push("API_PORT missing");
  if (!env.AUTH_PUBLIC_BASE_URL) configIssues.push("AUTH_PUBLIC_BASE_URL missing");
  record(`config: ${configIssues.length === 0 ? "ok" : configIssues.join(", ")}`);
  record(`config web=${env.WEB_PORT} api=${env.API_PORT} public=${env.AUTH_PUBLIC_BASE_URL}`);

  const state = await readDeploymentState(root);
  const managedPorts = collectManagedPorts(env, state);
  const portServices = [
    { label: "web", port: env.WEB_PORT },
    { label: "api", port: env.API_PORT }
  ];

  const probe = options.probe ?? ((host, port) => probePort(host, port));
  for (const service of portServices) {
    const port = Number(service.port);
    if (!Number.isInteger(port)) {
      record(`port ${service.label}: invalid (${service.port})`);
      continue;
    }
    const result = await probe("0.0.0.0", port);
    if (result.available) {
      record(`port ${service.label} ${port}: available`);
    } else if (managedPorts.has(port)) {
      record(`port ${service.label} ${port}: in-use (managed)`);
    } else {
      record(`port ${service.label} ${port}: in-use (${result.owner ?? "unknown"})`);
    }
  }

  const storageRoot = path.join(root, env.STORAGE_ROOT_DIR || "storage");
  const envPath = path.join(root, ".env");
  const storageWritability = await inspectWritablePath(storageRoot);
  if (storageWritability.writable) {
    record(
      `permissions storage: writable (${storageWritability.exists ? storageRoot : storageWritability.checkedPath})`
    );
  } else {
    record(
      `permissions storage: not writable (${storageWritability.error ?? storageWritability.checkedPath})`
    );
  }
  record(`permissions .env: ${existsSync(envPath) ? "present" : "missing"}`);

  try {
    const diskPath = existsSync(storageRoot) ? storageRoot : root;
    const stats = await statfs(diskPath);
    const freeGiB = ((stats.bfree * stats.bsize) / (1024 ** 3)).toFixed(1);
    record(`disk free: ${freeGiB} GiB (${diskPath})`);
  } catch (error) {
    record(`disk: unavailable (${error.message})`);
  }

  if (state?.pid) {
    const alive = isProcessAlive(state.pid);
    record(`pid: ${state.pid} status=${state.status ?? "unknown"} alive=${alive}`);
  } else {
    record("pid: none");
  }

  if (state?.pid && isProcessAlive(state.pid)) {
    const summary = await collectDeploymentHealth(
      {
        processAlive: true,
        apiBaseUrl: `http://127.0.0.1:${env.API_PORT}`,
        webUrl: `http://127.0.0.1:${env.WEB_PORT}`
      },
      options.healthOptions
    );
    record(
      `health process=${summary.process} api=${summary.apiHealth}/${summary.apiReady} web=${summary.web}`
    );
  } else {
    record("health: skipped (process not running)");
  }

  return { lines, env, state };
}

export function formatDeploymentFailure(failure) {
  const summary = failure.summary ?? failure.error?.message ?? failure.stage;
  const lines = [`✗ ${summary}`];
  if (failure.maintenanceWindow) {
    lines.push("更新处于维护窗口，服务当前已停止；现有数据未被修改。");
  } else if (failure.configurationChanged) {
    lines.push("配置可能已更新；现有数据未被修改。");
  } else {
    lines.push("现有数据未被修改。");
  }
  lines.push(`完整日志：${failure.logPath}`);
  lines.push(`修复后重试：${failure.retryCommand}`);
  lines.push(`诊断命令：${failure.doctorCommand}`);
  if (failure.error?.message && failure.error.message !== summary) {
    lines.push(redactSensitiveText(failure.error.message));
  }
  return lines.join("\n");
}

export async function main(argv = process.argv.slice(2), options = {}) {
  const root = options.root ?? ROOT;
  let parsed;
  try {
    parsed = parseDeployArgs(argv);
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    return 2;
  }

  const print = options.print ?? ((message) => process.stdout.write(`${message}\n`));
  const ask = options.ask ?? createReadlineAsk();
  try {
    if (parsed.command === "help") {
      print(deploymentHelp());
      return 0;
    }
    const controller = createDeploymentController(createRealDeps({ root, parsed, ask, print }));
    await controller.run(parsed);
    return 0;
  } catch (error) {
    if (!error?.failure) print(redactSensitiveText(error.message ?? String(error)));
    return 1;
  } finally {
    ask.close?.();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = await main(process.argv.slice(2));
}
