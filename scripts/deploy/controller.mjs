export function createDeploymentController(deps) {
  async function runDeploy(parsed) {
    let configurationChanged = false;
    let serviceStopped = false;
    let stage = "load-config";

    try {
      let config = await deps.loadConfiguration();

      stage = "check-dependencies";
      await deps.checkDependencies(config, parsed);

      stage = "preflight";
      await deps.preflight(config, parsed);

      stage = "configure";
      config = await deps.configure(config, parsed);

      stage = "verify-ports";
      await deps.verifyPorts(config, { retries: 0 });

      stage = "write-config";
      await deps.writeConfiguration(config, parsed);
      configurationChanged = true;

      const running = await deps.isRunning();
      if (running) {
        stage = "stop-old";
        await deps.stop();
        serviceStopped = true;
      }

      stage = "install";
      await deps.installProject(config, parsed);

      stage = "build-web";
      await deps.buildWeb(config, parsed);

      stage = "build-tui";
      await deps.buildTui(config, parsed);

      stage = "verify-ports-again";
      await deps.verifyPorts(config);

      stage = "start";
      await deps.start(config, parsed);

      stage = "wait-for-health";
      await deps.waitForHealth(config, parsed);

      stage = "mark-healthy";
      await deps.markHealthy(config, parsed);

      return { ok: true };
    } catch (error) {
      const failure = {
        stage,
        summary: error?.message ?? String(error),
        error,
        configurationChanged,
        serviceStopped
      };
      await deps.reportFailure(failure);
      const wrapped = new Error(error?.message ?? String(error));
      wrapped.failure = failure;
      throw wrapped;
    }
  }

  async function runStart(restart = false) {
    const config = await deps.loadConfiguration();
    await deps.preflight(config, { command: "start" });
    await deps.checkDependencies(config, { command: "start" });
    if (restart) {
      await deps.verifyPorts(config, { retries: 0 });
      await deps.stop();
      await deps.verifyPorts(config);
    }
    await deps.start(config, { command: "start" });
    await deps.waitForHealth(config, { command: "start" });
    await deps.markHealthy(config, { command: "start" });
    return { ok: true };
  }

  async function runStop() {
    await deps.stop();
    return { ok: true };
  }

  async function runTui(parsed) {
    const config = await deps.loadConfiguration();
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
          return runStart(true);
        case "status":
          return deps.status();
        case "logs":
          return deps.logs();
        case "doctor":
          return deps.doctor();
        case "tui":
          return runTui(parsed);
        default:
          throw new Error(`Unknown command: ${parsed.command}`);
      }
    }
  };
}
