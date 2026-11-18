import { spawn } from "node:child_process";
import { constants, existsSync } from "node:fs";
import { access, mkdir, readFile, stat, statfs, writeFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { httpOrigin, readServerListen, writeServerListen } from "../server-listen.mjs";
import { captureOutput, createLogger, logPath } from "../lib/logging.mjs";
import { deploymentHelp, parseDeployArgs } from "./args.mjs";
import {
  deploymentConfigGaps,
  ensureDeploymentEnvironment,
  isCompleteDeploymentConfig,
  parseDeploymentEnvironment,
  redactSensitiveText,
  updateDeploymentEnvironment,
  writeDeploymentConfiguration
} from "./config.mjs";
import { createDeploymentController } from "./controller.mjs";
import { ensureDependencies, inspectDependencies } from "./dependencies.mjs";
import { collectDeploymentHealth, probeHttp, waitForDeployment } from "./health.mjs";
import { probePort, selectDeploymentPort, verifySelectedPorts } from "./ports.mjs";
import {
  healStaleDeploymentState,
  inspectManagedRuntime,
  isProcessAlive,
  readDeploymentState,
  startManagedStack,
  stopManagedStack,
  verifyManagedProcessForStop,
  writeDeploymentState
} from "./process-state.mjs";
const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "../..");
const TUI_ENTRY = "apps/tui/dist/index.js";

/** Host and port recorded when the stack started. An incomplete record has no snapshot. */
export function apiListenFromState(state) {
  const host = typeof state?.listen?.apiHost === "string" ? state.listen.apiHost.trim() : "";
  const port = state?.ports?.api;
  if (!host || port == null || port === "") return null;
  const parsed = Number(port);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) return null;
  return { host, port: String(parsed) };
}

function clientListenHost(host) {
  const value = String(host ?? "").trim();
  if (value === "0.0.0.0") return "127.0.0.1";
  if (value === "::" || value === "::0") return "::1";
  return value;
}

export function resolveTuiRuntimeUrl(env = {}, runtimeUrl = null, apiListen = null) {
  const explicit = String(runtimeUrl ?? "").trim();
  if (explicit) return explicit;
  const listen = apiListen ?? readServerListen(env);
  return `${httpOrigin(clientListenHost(listen.host), listen.port)}/api/v1/runs`;
}

export function healthProbeUrls(env = {}, web = {}, apiListen = null) {
  const api = apiListen ?? readServerListen(env);
  return {
    apiBaseUrl: httpOrigin(api.host, api.port),
    webUrl: httpOrigin(web.host || "127.0.0.1", web.port || "3000")
  };
}

export function webListenFromState(state, fallback = { host: "127.0.0.1", port: "3000" }) {
  if (state?.ports?.web == null || state.ports.web === "") {
    return { host: fallback.host || "127.0.0.1", port: String(fallback.port || "3000") };
  }
  return {
    host: state.listen?.webHost || "127.0.0.1",
    port: String(state.ports.web)
  };
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

export function webListenFromArgs(parsed = {}) {
  const host = String(parsed.webHost ?? "").trim() || "127.0.0.1";
  const port = String(parsed.webPort ?? "").trim() || "3000";
  if (/\s/u.test(host)) throw new Error("--web-host must be a host name or address");
  const parsedPort = Number(port);
  if (!Number.isInteger(parsedPort) || parsedPort < 1 || parsedPort > 65535) {
    throw new Error("--web-port must be an integer between 1 and 65535");
  }
  return { host, port: String(parsedPort) };
}

export function managedWebStartArgs(web = {}, configPath) {
  const webHost = String(web.host || "127.0.0.1").trim() || "127.0.0.1";
  const webPort = String(web.port || "3000").trim() || "3000";
  const args = ["run", "start", "--", "--web-host", webHost, "--web-port", webPort];
  if (configPath) args.push("--config", configPath);
  return args;
}

/** Managed ports come from deployment.json when the pid is alive and launchId matches. */
export async function collectManagedPorts(env = {}, state = null, options = {}) {
  const ports = new Set();
  void env;

  if (!state?.ports || typeof state.ports !== "object") return ports;
  if (!state?.pid) return ports;

  const alive = options.isAlive ?? isProcessAlive;
  if (!alive(state.pid)) return ports;

  if (process.platform === "linux" || options.forceLaunchIdCheck) {
    const verification = await verifyManagedProcessForStop(state.pid, state.launchId, {
      readLaunchId: options.readLaunchId,
      forceLaunchIdCheck: options.forceLaunchIdCheck
    });
    if (!verification.allowed) return ports;
  }

  for (const value of Object.values(state.ports)) {
    const port = Number(value);
    if (Number.isInteger(port) && port > 0) ports.add(port);
  }
  return ports;
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
    processEnv,
    write = false,
    timestamp
  } = options;

  const listenEnv = processEnv ?? process.env;
  const configPath = typeof options.configPath === "string" ? options.configPath.trim() : "";
  const selectedListen = readServerListen(listenEnv, configPath || undefined);
  let text = sourceText ?? "";
  const originalEnv = parseDeploymentEnvironment(text);
  const completeBeforeFill = isCompleteDeploymentConfig(originalEnv);
  let ensured = ensureDeploymentEnvironment(text);
  text = ensured.text;
  let env = ensured.env;
  const savedWeb = options.web ?? { host: "127.0.0.1", port: "3000" };

  if (
    !reconfigure &&
    !nonInteractive &&
    completeBeforeFill &&
    sourceText?.trim()
  ) {
    return {
      env,
      envText: text,
      wrote: false,
      listen: { host: selectedListen.host, port: selectedListen.port },
      web: savedWeb
    };
  }

  const deploymentState = root ? await readDeploymentState(root).catch(() => null) : null;
  const managedPorts = await collectManagedPorts(
    env,
    deploymentState,
    options.collectManagedPortsOptions ?? {}
  );

  const reserved = new Set();
  if (configPath) reserved.add(Number(selectedListen.port));
  const webPort = await selectPortWithHint({
    label: "Web",
    defaultPort: Number(savedWeb.port || 3000),
    reserved,
    managedPorts,
    nonInteractive,
    ask,
    print,
    probe
  });
  reserved.add(webPort);
  const apiPort = configPath
    ? Number(selectedListen.port)
    : await selectPortWithHint({
        label: "API",
        defaultPort: Number(selectedListen.port),
        reserved,
        managedPorts,
        nonInteractive,
        ask,
        print,
        probe
      });
  if (!configPath) reserved.add(apiPort);

  const web = { host: savedWeb.host || "127.0.0.1", port: String(webPort) };
  const listen = { host: selectedListen.host, port: String(apiPort) };

  ensured = ensureDeploymentEnvironment(text);
  text = ensured.text;
  env = ensured.env;

  let wrote = false;
  if (write && root) {
    await writeDeploymentConfiguration(root, text, {
      backup: Boolean(reconfigure),
      timestamp
    });
    wrote = true;
  }

  return { env, envText: text, wrote, listen, web };
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

export function formatStartBlocked(gaps, missingFiles = []) {
  const parts = [];
  if (gaps.length > 0) {
    parts.push(`缺少合法完整的 .env 配置，缺：${gaps.join(", ")}`);
  }
  if (missingFiles.length > 0) {
    parts.push(`缺少文件：${missingFiles.join(", ")}`);
  }
  if (parts.length === 0) return "";
  return `${parts.join("；")}。请先运行 ./deploy.sh deploy`;
}

/** Start refuses a missing web build. */
export async function prepareStartEnvironment(root, diskEnv = {}) {
  const gaps = deploymentConfigGaps(diskEnv);
  const missingFiles = [];
  if (!existsSync(path.join(root, "apps/web/.next/BUILD_ID"))) {
    missingFiles.push("apps/web/.next/BUILD_ID");
  }
  if (gaps.length > 0 || missingFiles.length > 0) {
    throw new Error(formatStartBlocked(gaps, missingFiles));
  }
}

function runCommand(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd ?? ROOT,
      env: options.env ?? process.env,
      stdio: options.logger ? ["ignore", "pipe", "pipe"] : (options.stdio ?? "inherit")
    });

    const outputs = [];
    if (options.logger) {
      const tee = (stream, target) => {
        const output = captureOutput(options.logger, { stage: options.stage, echo: target });
        outputs.push(output);
        stream.setEncoding("utf8").on("data", chunk => output.append(chunk));
      };
      tee(child.stdout, process.stdout);
      tee(child.stderr, process.stderr);
    }

    child.on("error", reject);
    child.on("close", (code) => {
      for (const output of outputs) output.flush();
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
  const loggingEnv = () => ({ ...process.env, ...config?.env });

  const commandOptions = (file = "backend", stage = "install") => {
    const logger = createLogger({ file, component: "launcher", env: loggingEnv() });
    return { cwd: root, logger, stage, logPath: logger.path };
  };

  return {
    async loadConfiguration() {
      const sourceText = await loadRootEnvText(root);
      const ensured = ensureDeploymentEnvironment(sourceText);
      config = {
        env: ensured.env,
        envText: ensured.text,
        sourceText,
        generatedKeys: ensured.generatedKeys,
        web: webListenFromArgs(parsed),
        ask,
      };
      return config;
    },
    async preflight(current, options) {
      if (options?.command === "start") {
        const diskEnv = parseDeploymentEnvironment(current.sourceText ?? "");
        await prepareStartEnvironment(root, diskEnv);
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
        write: false,
        web: webListenFromArgs(parsed),
        configPath: parsed.config
      });
      config = { ...current, ...configured };
      return config;
    },
    async checkDependencies() {
      await ensureDependencies({
        nonInteractive: parsed.nonInteractive,
        ask,
        print,
        install: async (action, { nonInteractive } = {}) => runCommand("bash", [
          path.join(root, "scripts/deploy/install-dependency.sh"), action,
          ...(nonInteractive ? ["--non-interactive"] : []),
        ], commandOptions()),
      });
    },
    async selectPorts(current) {
      return current;
    },
    async writeConfiguration(current) {
      await writeDeploymentConfiguration(root, current.envText, {
        backup: parsed.reconfigure,
        // Redeploy without --reconfigure must not silently lose disk secrets.
        backupExistingSecrets: !parsed.reconfigure
      });
      if (current.listen && !parsed.config) {
        writeServerListen(process.env, current.listen);
      }
    },
    async isRunning() {
      const inspection = await inspectManagedRuntime(root);
      if (inspection.stale) {
        await healStaleDeploymentState(root);
        return false;
      }
      return inspection.running;
    },
    async stop() {
      const env = { ...process.env, ...parseDeploymentEnvironment(await loadRootEnvText(root).catch(() => "")) };
      createLogger({ component: "launcher", env }).info("Stopping managed stack", { stage: "stop" });
      await stopManagedStack(root);
    },
    async installProject() {
      const options = commandOptions();
      // Prepare Python once with the service runtime selection after npm ci.
      await runCommand("npm", ["ci"], {
        ...options, env: { ...process.env, ...options.env, SKIP_POSTINSTALL_BUILD: "1" }
      });
      await runCommand(process.execPath, [
        path.join(root, "scripts/python-environment.mjs"), "--no-dev", "--extra", "video"
      ], options);
    },
    async buildWeb() {
      await runCommand("npm", ["run", "build:web"], commandOptions("web", "build"));
    },
    async buildTui() {
      await runCommand("npm", ["run", "build:tui"], commandOptions("tui", "build"));
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
      const state = await readDeploymentState(root).catch(() => null);
      const apiBaseUrl = healthProbeUrls(current.env, current.web, apiListenFromState(state)).apiBaseUrl;
      const health = await probeHttp(`${apiBaseUrl}/healthz`);
      if (!(health.ok && health.status === 200)) {
        throw new Error(
          `API is not healthy at ${apiBaseUrl}/healthz. Start the stack with ./deploy.sh start (or deploy), then retry ./deploy.sh tui.`
        );
      }
    },
    async startTui(current, parsed) {
      ask.close?.();
      const state = await readDeploymentState(root).catch(() => null);
      const runtimeUrl = resolveTuiRuntimeUrl(
        current.env,
        parsed?.runtimeUrl,
        apiListenFromState(state),
      );
      print(`Starting TUI against ${runtimeUrl} (foreground client; Ctrl+C exits TUI only).`);
      await runCommand(process.execPath, [path.join(root, TUI_ENTRY), "--runtime-url", runtimeUrl], {
        cwd: root,
        stdio: "inherit"
      });
    },

    async verifyPorts(current) {
      const web = current.web ?? { host: "127.0.0.1", port: "3000" };
      const api = readServerListen(current.env, parsed.config);
      const services = [
        { label: "Web", host: web.host, port: web.port },
        { label: "API", host: api.host, port: api.port }
      ];
      // Only verified running managed ports are owned. After stop-old, TIME_WAIT is
      // handled by short retries inside verifySelectedPorts — not by treating .env as owned.
      const state = await readDeploymentState(root).catch(() => null);
      const managedPorts = await collectManagedPorts(current.env, state);
      await verifySelectedPorts(services, {
        managedPorts,
        retries: 5,
        retryDelayMs: 200
      });
    },
    async start(current) {
      const web = current.web ?? webListenFromArgs(parsed);
      const args = managedWebStartArgs(web, parsed.config);
      const api = readServerListen(current.env, parsed.config);
      await startManagedStack(root, {
        env: { ...process.env, ...current.env },
        args,
        ports: {
          web: Number(web.port),
          api: Number(api.port)
        },
        listen: { webHost: web.host, apiHost: api.host }
      });
    },
    async waitForHealth(current) {
      try {
        const web = current.web ?? webListenFromArgs(parsed);
        const api = readServerListen(current.env, parsed.config);
        return await waitForDeployment({
          checkProcessAlive: async () => {
            const inspection = await inspectManagedRuntime(root);
            return inspection.running;
          },
          ...healthProbeUrls(current.env, web, api)
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
      const inspection = await inspectManagedRuntime(root);
      if (!inspection.running || !inspection.state?.pid) {
        throw new Error("Managed process is not alive; refusing to mark deployment healthy");
      }
      await writeDeploymentState(root, { ...inspection.state, status: "healthy" });
      const web = current.web ?? { host: "127.0.0.1", port: "3000" };
      const api = readServerListen(current.env, parsed.config);
      const probes = healthProbeUrls(current.env, web, api);
      const webUrl = probes.webUrl;
      print(`DataGallery is healthy at ${webUrl}`);
      const ready = await probeHttp(`${probes.apiBaseUrl}/ready`);
      if (!(ready.ok && ready.status === 200)) {
        print("Next: put LLM_MODEL, LLM_BASE_URL and LLM_API_KEY in .env, then run ./deploy.sh restart and open the Web UI.");
      } else {
        print(`Open the Web UI at ${webUrl}`);
      }
      print("TUI is built and ready. Start it in another terminal (foreground client, not a background service):");
      print("  ./deploy.sh tui");
    },

    async status() {
      const inspection = await inspectManagedRuntime(root);
      const state = inspection.state;
      const env =
        config?.env ??
        ensureDeploymentEnvironment(await loadRootEnvText(root)).env;
      const web = inspection.running
        ? webListenFromState(state, config?.web ?? webListenFromArgs(parsed))
        : (config?.web ?? webListenFromArgs(parsed));
      const summary = await collectDeploymentHealth({
        checkProcessAlive: async () => inspection.running,
        ...healthProbeUrls(env, web, apiListenFromState(state))
      });
      print(`进程        ${summary.process}`);
      print(`API         ${summary.apiHealth}/${summary.apiReady}`);
      print(`Web         ${summary.web}`);
      return summary;
    },
    async logs() {
      const env = { ...process.env, ...parseDeploymentEnvironment(await loadRootEnvText(root).catch(() => "")) };
      const paths = ["backend", "web", "tui"].map(file => logPath(file, env));
      for (const file of paths) { await mkdir(path.dirname(file), { recursive: true }); if (!existsSync(file)) await writeFile(file, "", { mode: 0o600 }); }
      await runCommand("tail", ["-n", "200", "-F", ...paths]);
    },
    async doctor() {
      await runDeploymentDoctor(root, { print, web: webListenFromArgs(parsed) });
    },
    async printHelp(text) {
      print(text);
    },
    async reportFailure(failure) {
      const file = failure.stage === "build-web" ? "web" : failure.stage === "build-tui" ? "tui" : "backend";
      const logger = createLogger({ file, component: "launcher", env: loggingEnv() });
      logger.error("Deployment failed", failure.error, { stage: failure.stage });
      failure.logPath = logger.path;
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
  const ensured = ensureDeploymentEnvironment(envText);
  const env = ensured.env;

  const deps = await inspectDependencies({ run: options.run });
  for (const entry of deps) {
    record(
      `dependency ${entry.name}: ${entry.status}${entry.foundVersion ? ` (${entry.foundVersion})` : ""}`
    );
  }

  const inspection = await inspectManagedRuntime(root);
  const state = inspection.state;
  const api = apiListenFromState(state) ?? readServerListen(env);
  const web = inspection.running
    ? webListenFromState(state, options.web ?? { host: "127.0.0.1", port: "3000" })
    : (options.web ?? { host: "127.0.0.1", port: "3000" });
  record("config: ok");
  record(
    `config web=${httpOrigin(web.host, web.port)} api=${httpOrigin(api.host, api.port)}`
  );

  const managedPorts = await collectManagedPorts(env, state);
  const portServices = [
    { label: "web", host: web.host, port: web.port },
    { label: "api", host: api.host, port: api.port }
  ];

  const probe = options.probe ?? ((host, port) => probePort(host, port));
  for (const service of portServices) {
    const port = Number(service.port);
    if (!Number.isInteger(port)) {
      record(`port ${service.label}: invalid (${service.port})`);
      continue;
    }
    const result = await probe(service.host || "0.0.0.0", port);
    if (result.available) {
      record(`port ${service.label} ${port}: available`);
    } else if (managedPorts.has(port)) {
      record(`port ${service.label} ${port}: in-use (managed)`);
    } else {
      record(`port ${service.label} ${port}: in-use (${result.owner ?? "unknown"})`);
    }
  }

  const storageRoot = path.join(root, "storage");
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
  if (existsSync(envPath)) {
    try {
      const mode = (await stat(envPath)).mode & 0o777;
      const modeText = mode.toString(8).padStart(3, "0");
      if (mode === 0o600) {
        record(`permissions .env: present mode=${modeText}`);
      } else {
        record(
          `permissions .env: present mode=${modeText} (expected 600; run: chmod 600 .env)`
        );
      }
    } catch (error) {
      record(`permissions .env: present (mode unavailable: ${error.message})`);
    }
  } else {
    record("permissions .env: missing");
  }

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
    record(
      `pid: ${state.pid} status=${state.status ?? "unknown"} alive=${alive} managed=${inspection.running}`
    );
    if (inspection.stale) {
      record(
        `pid: stale (${inspection.reason}). Fix: ./deploy.sh stop  # clears stale state without signaling foreign pid; then ./deploy.sh start`
      );
    }
  } else if (inspection.stale) {
    record(`pid: stale state (${inspection.reason}). Fix: ./deploy.sh stop && ./deploy.sh start`);
  } else {
    record("pid: none");
  }

  if (inspection.running) {
    const summary = await collectDeploymentHealth(
      {
        processAlive: true,
        ...healthProbeUrls(env, web, api)
      },
      options.healthOptions
    );
    record(
      `health process=${summary.process} api=${summary.apiHealth}/${summary.apiReady} web=${summary.web}`
    );
  } else {
    record("health: skipped (process not running)");
  }

  return { lines, env, state, inspection };
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
    if (!error?.failure) {
      const env = { ...process.env, ...parseDeploymentEnvironment(await loadRootEnvText(root).catch(() => "")) };
      createLogger({ file: parsed.command === "tui" ? "tui" : "backend", component: "launcher", env })
        .error("Service command failed", error, { stage: parsed.command });
      print(redactSensitiveText(error.message ?? String(error)));
    }
    return 1;
  } finally {
    ask.close?.();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = await main(process.argv.slice(2));
}
