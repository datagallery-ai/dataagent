import { deploymentHelp } from "./args.mjs";

export function createDeploymentController(deps) {
  async function runDeploy(parsed) {
    const calls = [];
    let configurationChanged = false;
    let serviceStopped = false;
    let stage = "load-config";
    let config;
    let logPath = "storage/logs/deploy-latest.log";

    try {
      if (deps.beginDeployLog) {
        logPath = await deps.beginDeployLog();
      }

      stage = "load-config";
      config = await deps.loadConfiguration({ reconfigure: parsed.reconfigure });
      calls.push("load-config");

      stage = "preflight";
      await deps.preflight(config, parsed);
      calls.push("preflight");

      stage = "configure";
      config = await deps.configure(config, parsed);
      calls.push("configure");

      stage = "check-dependencies";
      await deps.checkDependencies(config, parsed);
      calls.push("check-dependencies");

      stage = "select-ports";
      config = await deps.selectPorts(config, parsed);
      calls.push("select-ports");

      stage = "write-config";
      await deps.writeConfiguration(config, parsed);
      configurationChanged = true;
      calls.push("write-config");

      const running = await deps.isRunning();
      if (running) {
        stage = "stop-old";
        await deps.stop();
        serviceStopped = true;
        calls.push("stop-old");
      }

      stage = "install";
      await deps.installProject(config, parsed);
      calls.push("install");

      stage = "build-typescript";
      await deps.buildTypeScript(config, parsed);
      calls.push("build-typescript");

      stage = "build-web";
      await deps.buildWeb(config, parsed);
      calls.push("build-web");

      stage = "build-tui";
      await deps.buildTui(config, parsed);
      calls.push("build-tui");

      stage = "verify-ports-again";
      await deps.verifyPorts(config, parsed);
      calls.push("verify-ports-again");

      stage = "start";
      await deps.start(config, parsed);
      calls.push("start");

      stage = "wait-for-health";
      await deps.waitForHealth(config, parsed);
      calls.push("wait-for-health");

      stage = "mark-healthy";
      await deps.markHealthy(config, parsed);
      calls.push("mark-healthy");

      return { ok: true, calls, configurationChanged, serviceStopped };
    } catch (error) {
      const failure = {
        stage,
        summary: error?.message ?? String(error),
        error,
        configurationChanged,
        serviceStopped,
        maintenanceWindow: serviceStopped,
        oldServiceRunning: false,
        logPath: error?.logPath ?? logPath,
        retryCommand: "./deploy.sh deploy",
        doctorCommand: "./deploy.sh doctor",
        calls
      };
      await deps.reportFailure(failure);
      const wrapped = new Error(error?.message ?? String(error));
      wrapped.failure = failure;
      throw wrapped;
    }
  }

  async function runStart() {
    const config = await deps.loadConfiguration({ reconfigure: false });
    await deps.preflight(config, { command: "start" });
    await deps.start(config, { command: "start" });
    await deps.waitForHealth(config, { command: "start" });
    await deps.markHealthy(config, { command: "start" });
    return { ok: true };
  }

  async function runStop() {
    await deps.stop();
    return { ok: true };
  }

  async function runRestart() {
    await deps.stop();
    return runStart();
  }

  async function runTui(parsed) {
    const config = await deps.loadConfiguration({ reconfigure: false });
    await deps.ensureTuiReady(config, parsed);
    await deps.checkApiForTui(config, parsed);
    await deps.startTui(config, parsed);
    return { ok: true };
  }

  return {
    async run(parsed) {
      switch (parsed.command) {
        case "deploy":
          return runDeploy(parsed);
        case "start":
          return runStart();
        case "stop":
          return runStop();
        case "restart":
          return runRestart();
        case "status":
          return deps.status();
        case "logs":
          return deps.logs();
        case "doctor":
          return deps.doctor();
        case "tui":
          return runTui(parsed);
        case "help":
          return deps.printHelp(deploymentHelp());
        default:
          throw new Error(`Unknown command: ${parsed.command}`);
      }
    }
  };
}
