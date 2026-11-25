import { writeServerListen } from "./server-config.mjs";
import { execFile, spawn } from "node:child_process";
import { createRequire } from "node:module";
import { promisify } from "node:util";
import { constants, existsSync } from "node:fs";
import { access, mkdir, readFile, stat, statfs, writeFile } from "node:fs/promises";
import { createInterface } from "node:readline";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { httpOrigin, readServerListen, resolveHome } from "../../ui/shared/server-listen.mjs";
import { venvPython } from "../../ui/shared/python-environment.mjs";
import { captureOutput, createLogger, logPath } from "../../ui/shared/logging.mjs";
import { resolveWebListen } from "../stack-runtime-config.mjs";
import { deploymentHelp, parseDeployArgs } from "./args.mjs";
import {
  ensureDeploymentEnvironment,
  parseDeploymentEnvironment,
  redactSensitiveText,
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
const TUI_ENTRY = "ui/tui/dist/index.js";
const execFileAsync = promisify(execFile);

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
    host: state.listen?.webHost || fallback.host,
    port: String(state.ports.web)
  };
}

function deploymentTarget({ configPath, webHost, webPort }, previous) {
  const savedWeb = webListenFromState(previous);
  return {
    configPath: configPath ?? previous?.configPath,
    web: resolveWebListen({
      webHost: webHost ?? savedWeb.host,
      webPort: webPort ?? savedWeb.port
    })
  };
}

export async function inspectWritablePath(targetPath) {
  let candidate = path.resolve(targetPath);
  while (true) {
    try {
      if (!(await stat(candidate)).isDirectory()) throw new Error("Expected a directory");
      await access(candidate, constants.W_OK | constants.X_OK);
      return { writable: true, checkedPath: candidate, exists: candidate === path.resolve(targetPath) };
    } catch (error) {
      const parent = path.dirname(candidate);
      if (error.code !== "ENOENT" || parent === candidate) {
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

export function managedWebStartArgs(web = {}, configPath) {
  const { host, port } = resolveWebListen({ webHost: web.host, webPort: web.port });
  const args = ["--api", "--web", "--web-host", host, "--web-port", port];
  if (configPath) args.push("--config", configPath);
  return args;
}

/** Managed ports come from deployment.json when the pid is alive and launchId matches. */
export async function collectManagedPorts(state = null, options = {}) {
  const ports = new Set();

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

function sameListen(left, right) {
  return left != null && left.host === right.host && Number(left.port) === Number(right.port);
}

async function deploymentPortProbe(state, probe = probePort) {
  const managedPorts = await collectManagedPorts(state);
  return async (host, port, options) => {
    const result = await probe(host, port, options);
    if (result.code !== "EADDRINUSE" || !managedPorts.has(port)) return result;

    if (!result.listeners) {
      const saved = [webListenFromState(state), apiListenFromState(state)];
      return { ...result, managed: saved.some(listener => sameListen(listener, { host, port })) };
    }
    const owners = result.listeners.map(socket => socket.owners);
    if (!owners.length || owners.some(list => !list.length)) return result;
    const pids = new Set(owners.flat().map(owner => owner.pid));
    const verified = await Promise.all(
      [...pids].map(pid => verifyManagedProcessForStop(pid, state.launchId))
    );
    return { ...result, managed: verified.every(owner => owner.allowed) };
  };
}

function readDeploymentListen(env, configPath, previous) {
  const listen = readServerListen(env, configPath);
  const saved = apiListenFromState(previous);
  if (saved && previous.configPath === undefined && !configPath && !sameListen(saved, listen)) {
    throw new Error("The prior config path was not recorded and Home config uses a different API address. Supply --config to select the intended configuration.");
  }
  return listen;
}

async function validateBackendConfig(root, { env, configPath }) {
  try {
    await execFileAsync(venvPython(root, env), ["-B", "-c", `
import sys
from dataagent import LaunchOptions, prepare_runtime, safe_error
from dataagent.bootstrap.config_files import MissingEnvironmentError

try:
    prepare_runtime(LaunchOptions(config=sys.argv[1] or None), preview_home=True)
except MissingEnvironmentError:
    pass
except Exception as error:
    sys.exit(safe_error(error)["message"])
`, configPath ?? ""], { cwd: root, env, timeout: 10_000 });
  } catch (error) {
    const detail = error.stderr?.trim() || error.code || "Python exited unexpectedly";
    throw new Error(`Backend configuration check failed: ${redactSensitiveText(String(detail))}`);
  }
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
    processEnv
  } = options;

  const listenEnv = processEnv ?? process.env;
  const configPath = typeof options.configPath === "string" ? options.configPath.trim() : "";
  const selectedListen = readDeploymentListen(listenEnv, configPath || undefined, options.previous);
  const { text, env } = ensureDeploymentEnvironment(sourceText ?? "");
  const savedWeb = options.web ?? { host: "127.0.0.1", port: "3000" };

  if (!reconfigure && (options.previous || (!nonInteractive && sourceText?.trim()))) {
    return {
      env,
      envText: text,
      listen: { host: selectedListen.host, port: selectedListen.port },
      web: savedWeb
    };
  }

  const deploymentState = root ? await readDeploymentState(root).catch(() => null) : null;
  const checkPort = await deploymentPortProbe(
    deploymentState, probe && ((host, port) => probe(port, host))
  );

  const webPort = await selectPortWithHint({
    label: "Web",
    host: savedWeb.host,
    defaultPort: Number(savedWeb.port || 3000),
    reserved: configPath ? [{ ...selectedListen, allAddresses: true }] : [],
    nonInteractive: nonInteractive || options.fixedWebPort,
    ask,
    print,
    probe: port => checkPort(savedWeb.host, port)
  });
  const apiPort = configPath
    ? Number(selectedListen.port)
    : await selectPortWithHint({
        label: "API",
        host: selectedListen.host,
        allAddresses: true,
        defaultPort: Number(selectedListen.port),
        reserved: [{ host: savedWeb.host, port: webPort }],
        nonInteractive,
        ask,
        print,
        probe: port => checkPort(selectedListen.host, port, { allAddresses: true })
      });
  const web = { host: savedWeb.host || "127.0.0.1", port: String(webPort) };
  const listen = { host: selectedListen.host, port: String(apiPort) };

  return { env, envText: text, listen, web };
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

/** Lifecycle commands require existing Web and Python artifacts; only deploy prepares them. */
export async function prepareStartEnvironment(root, diskEnv = {}) {
  const missingFiles = [];
  if (!existsSync(path.join(root, "ui/web/.next/BUILD_ID"))) {
    missingFiles.push("ui/web/.next/BUILD_ID");
  }
  const interpreter = venvPython(path.join(root, "."), { ...diskEnv, ...process.env });
  if (!existsSync(interpreter)) missingFiles.push(interpreter);
  if (missingFiles.length > 0) {
    throw new Error(`缺少文件：${missingFiles.join(", ")}。请先运行 ./deploy.sh deploy`);
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
      else reject(new Error(`${command} ${args.join(" ")} failed with code ${code}`));
    });
  });
}

function createRealDeps(context) {
  const { root, parsed, ask, print } = context;

  let config = null;
  let nodeDependenciesInstalled = false;
  let pythonDependenciesInstalled = false;
  const loggingEnv = () => ({ ...config?.env, ...process.env });

  const commandOptions = (file = "backend", stage = "install") => {
    const logger = createLogger({ file, component: "launcher", env: loggingEnv() });
    return { cwd: root, env: loggingEnv(), logger, stage };
  };

  async function installFrontend() {
    if (nodeDependenciesInstalled) return;
    await runCommand("npm", ["install", "--include=dev"], commandOptions());
    nodeDependenciesInstalled = true;
  }

  async function installPython() {
    if (pythonDependenciesInstalled) return;
    await runCommand("uv", ["sync", "--no-dev", "--extra", "video"], commandOptions());
    pythonDependenciesInstalled = true;
  }

  return {
    async loadConfiguration() {
      const sourceText = await loadRootEnvText(root);
      const ensured = ensureDeploymentEnvironment(sourceText);
      const inspection = parsed.command === "deploy" || parsed.command === "restart"
        ? await inspectManagedRuntime(root)
        : null;
      const previous = inspection?.running ? inspection.state : null;
      config = {
        env: { ...ensured.env, ...process.env },
        envText: ensured.text,
        sourceText,
        previous,
        ...deploymentTarget({ ...parsed, configPath: parsed.config }, previous),
      };
      return config;
    },
    async preflight(current, options) {
      if (options?.command === "deploy") {
        print("预检：检查部署目录、配置和监听端口。");
        for (const directory of [root, path.join(root, "storage/run"), resolveHome(current.env),
          path.dirname(logPath("backend", current.env))]) {
          const result = await inspectWritablePath(directory);
          if (!result.writable) {
            throw new Error(`Deployment directory is not writable: ${result.checkedPath} (${result.error})`);
          }
        }
        const require = createRequire(path.join(root, "package.json"));
        try { require.resolve("yaml"); }
        catch (error) {
          if (error.code !== "MODULE_NOT_FOUND") throw error;
          // A fresh checkout needs the YAML parser before reading configuration.
          await installFrontend();
        }
      }
      if (options?.command === "start") {
        await prepareStartEnvironment(root, current.env);
        readDeploymentListen(current.env, current.configPath, current.previous);
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
        processEnv: current.env,
        web: current.web,
        fixedWebPort: parsed.webPort != null,
        configPath: current.configPath,
        previous: current.previous
      });
      config = { ...current, ...configured, env: { ...configured.env, ...process.env } };
      if (current.previous) {
        if (!existsSync(venvPython(root, config.env))) await installPython();
        await validateBackendConfig(root, config);
      }
      return config;
    },
    async checkDependencies(current, options) {
      if (options?.command === "start") {
        const entries = await inspectDependencies({ includeUv: false });
        const missing = entries.filter(entry => entry.status !== "ok");
        if (missing.length) {
          throw new Error(`Missing dependencies: ${missing.map(entry => entry.name).join(", ")}. Run ./deploy.sh deploy to install them.`);
        }
        await validateBackendConfig(root, current);
        return;
      }
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
    async writeConfiguration(current) {
      await writeDeploymentConfiguration(root, current.envText, {
        backup: parsed.reconfigure,
        // Redeploy without --reconfigure must not silently lose disk secrets.
        backupExistingSecrets: !parsed.reconfigure
      });
      if (current.listen && !current.configPath) {
        writeServerListen(current.env, current.listen);
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
      const env = { ...parseDeploymentEnvironment(await loadRootEnvText(root).catch(() => "")), ...process.env };
      createLogger({ component: "launcher", env }).info("Stopping managed stack", { stage: "stop" });
      await stopManagedStack(root);
    },
    async installProject() {
      await installFrontend();
      await installPython();
      if (!config.previous) await validateBackendConfig(root, config);
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
    async startTui(current, parsed) {
      const entry = path.join(root, TUI_ENTRY);
      if (!existsSync(entry)) {
        throw new Error(`TUI is not built (${TUI_ENTRY}). Run ./deploy.sh deploy first.`);
      }
      ask.close?.();
      const explicit = parsed.runtimeUrl?.trim();
      const state = explicit ? null : await readDeploymentState(root).catch(() => null);
      const runtimeUrl = resolveTuiRuntimeUrl(current.env, explicit, apiListenFromState(state));
      print(`Starting TUI against ${runtimeUrl} (foreground client; Ctrl+C exits TUI only).`);
      await runCommand(process.execPath, [entry, "--runtime-url", runtimeUrl], {
        cwd: root,
        env: current.env,
        stdio: "inherit"
      });
    },

    async verifyPorts(current, { retries = 5 } = {}) {
      const web = current.web;
      const api = current.listen ?? readServerListen(current.env, current.configPath);
      const services = [
        { label: "Web", host: web.host, port: web.port },
        { label: "API", host: api.host, port: api.port, allAddresses: true }
      ];
      const state = await readDeploymentState(root).catch(() => null);
      const probe = await deploymentPortProbe(state);
      await verifySelectedPorts(services, {
        probe: (port, host, options) => probe(host, port, options),
        retries,
      });
      if (parsed.command === "deploy" && retries === 0) {
        print(`预检通过。Web ${httpOrigin(web.host, web.port)}，API ${httpOrigin(api.host, api.port)}。`);
      }
    },
    async start(current) {
      const web = current.web;
      const args = managedWebStartArgs(web, current.configPath);
      const api = readServerListen(current.env, current.configPath);
      await startManagedStack(root, {
        env: { ...current.env, ...process.env },
        args,
        ports: {
          web: Number(web.port),
          api: Number(api.port)
        },
        listen: { webHost: web.host, apiHost: api.host },
        configPath: current.configPath
      });
    },
    async waitForHealth(current) {
      try {
        const web = current.web;
        const api = readServerListen(current.env, current.configPath);
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
      const web = current.web;
      const api = readServerListen(current.env, current.configPath);
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
        { ...ensureDeploymentEnvironment(await loadRootEnvText(root)).env, ...process.env };
      const defaultWeb = config?.web ?? resolveWebListen(parsed);
      const web = inspection.running ? webListenFromState(state, defaultWeb) : defaultWeb;
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
      const env = { ...parseDeploymentEnvironment(await loadRootEnvText(root).catch(() => "")), ...process.env };
      const paths = ["backend", "web", "tui"].map(file => logPath(file, env));
      for (const file of paths) { await mkdir(path.dirname(file), { recursive: true }); if (!existsSync(file)) await writeFile(file, "", { mode: 0o600 }); }
      await runCommand("tail", ["-n", "200", "-F", ...paths]);
    },
    async doctor() {
      await runDeploymentDoctor(root, { print, configPath: parsed.config, webHost: parsed.webHost, webPort: parsed.webPort });
    },
    async reportFailure(failure) {
      const file = failure.stage === "build-web" ? "web" : failure.stage === "build-tui" ? "tui" : "backend";
      const logger = createLogger({ file, component: "launcher", env: loggingEnv() });
      logger.error("Deployment failed", failure.error, { stage: failure.stage });
      for (const line of formatDeploymentFailure(failure, logger.path).split("\n")) {
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
  const sourceEnv = options.env ?? process.env;
  const env = { ...ensured.env, ...sourceEnv };

  const deps = await inspectDependencies({ run: options.run });
  for (const entry of deps) {
    record(
      `dependency ${entry.name}: ${entry.status}${entry.foundVersion ? ` (${entry.foundVersion})` : ""}`
    );
  }

  const run = options.run ?? execFileAsync;
  const sources = [];
  try {
    const registry = await run("npm", ["config", "get", "registry"], { cwd: root, env, timeout: 5000 });
    sources.push(["npm", registry.stdout.trim()]);
  } catch {
    record("source npm: unavailable (run npm config get registry)");
  }
  for (const key of ["UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_PYTHON_INSTALL_MIRROR", "DATAAGENT_NODE_PACKAGE_URL"]) {
    // Node bootstrap runs before the project .env can be loaded.
    const value = key === "DATAAGENT_NODE_PACKAGE_URL" ? sourceEnv[key] : env[key];
    if (value) sources.push([key, value]);
  }
  for (const [name, value] of sources) {
    try {
      const url = new URL(value);
      const headers = {};
      if (url.username || url.password) {
        headers.Authorization = `Basic ${Buffer.from(`${decodeURIComponent(url.username)}:${decodeURIComponent(url.password)}`).toString("base64")}`;
      }
      url.username = url.password = "";
      const label = url.origin + url.pathname;
      const response = await (options.fetch ?? fetch)(url, { method: "HEAD", headers, signal: AbortSignal.timeout(5000) });
      await response.body?.cancel();
      record(`source ${name}: ${label} HTTP ${response.status} (endpoint response only)`);
    } catch {
      record(`source ${name}: unreachable or invalid URL`);
    }
  }
  try {
    const python = env.UV_PYTHON || "3.12";
    const result = await run("uv", ["python", "find", python, "--no-python-downloads", "--show-version"], { cwd: root, env, timeout: 5000 });
    record(`python local: ${result.stdout.trim()}`);
  } catch {
    record("python local: missing (set UV_PYTHON to a Python 3.12 executable, or configure UV_PYTHON_INSTALL_MIRROR)");
  }

  const inspection = await inspectManagedRuntime(root);
  const state = inspection.state;
  const running = inspection.running ? state : null;
  const { configPath, web } = deploymentTarget(options, running);
  let api;
  try {
    api = readDeploymentListen(env, configPath, running);
    if (existsSync(venvPython(root, env))) {
      await validateBackendConfig(root, { env, configPath });
      record("config: ok");
    } else {
      record("config: listen address checked (Python environment missing, backend schema skipped)");
    }
  } catch (error) {
    api = null;
    const detail = error.code === "MODULE_NOT_FOUND"
      ? "YAML parser missing. Run npm install --include=dev to check config.yaml."
      : redactSensitiveText(error.message);
    record(`config: unavailable (${detail})`);
  }
  record(
    `config web=${httpOrigin(web.host, web.port)} api=${api ? httpOrigin(api.host, api.port) : "unavailable"}`
  );

  const portServices = [
    { label: "web", ...web },
    ...(api ? [{ label: "api", ...api, allAddresses: true }] : [])
  ];

  const probe = await deploymentPortProbe(running, options.probe);
  for (const service of portServices) {
    const port = Number(service.port);
    if (!Number.isInteger(port)) {
      record(`port ${service.label}: invalid (${service.port})`);
      continue;
    }
    const result = await probe(service.host || "0.0.0.0", port, { allAddresses: service.allAddresses });
    if (result.available) {
      record(`port ${service.label} ${port}: available`);
    } else if (result.managed) {
      record(`port ${service.label} ${port}: in-use (managed)`);
    } else if (result.code && result.code !== "EADDRINUSE") {
      record(`port ${service.label} ${port}: unavailable (${result.code})`);
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

  if (inspection.running && api) {
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
    record(`health: skipped (${inspection.running ? "API configuration unavailable" : "process not running"})`);
  }

  return { lines, env, state, inspection };
}
export function formatDeploymentFailure(failure, logFile) {
  const summary = failure.summary ?? failure.error?.message ?? failure.stage;
  const lines = [`✗ ${summary}`];
  if (failure.serviceStopped) {
    lines.push("更新处于维护窗口，服务当前已停止；现有数据未被修改。");
  } else if (failure.configurationChanged) {
    lines.push("配置可能已更新；现有数据未被修改。");
  } else {
    lines.push("现有数据未被修改。");
  }
  lines.push(`完整日志：${logFile}`);
  lines.push("修复后重试：./deploy.sh deploy");
  lines.push("诊断命令：./deploy.sh doctor");
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
      const env = { ...parseDeploymentEnvironment(await loadRootEnvText(root).catch(() => "")), ...process.env };
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
