const COMMANDS = new Set([
  "deploy",
  "start",
  "stop",
  "restart",
  "status",
  "logs",
  "doctor",
  "tui",
  "help"
]);

export function parseDeployArgs(argv = []) {
  const tokens = [...argv];
  let command = "deploy";
  let reconfigure = false;
  let nonInteractive = false;
  let runtimeUrl = null;
  let sawCommand = false;

  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (token === "--reconfigure") {
      reconfigure = true;
      continue;
    }
    if (token === "--non-interactive") {
      nonInteractive = true;
      continue;
    }
    if (token === "--runtime-url") {
      const value = tokens[i + 1];
      if (!value || value.startsWith("-")) {
        throw new Error("--runtime-url requires a URL value");
      }
      runtimeUrl = value;
      i += 1;
      continue;
    }
    if (token.startsWith("-")) {
      throw new Error(`Unknown flag: ${token}`);
    }
    if (!COMMANDS.has(token)) {
      throw new Error(`Unknown command: ${token}`);
    }
    if (sawCommand) {
      throw new Error(`Unexpected extra command: ${token}`);
    }
    command = token;
    sawCommand = true;
  }

  if (reconfigure && nonInteractive) {
    throw new Error("--reconfigure and --non-interactive are mutually exclusive");
  }
  if (reconfigure && command !== "deploy") {
    throw new Error("--reconfigure is only valid with deploy");
  }
  if (nonInteractive && command !== "deploy") {
    throw new Error("--non-interactive is only valid with deploy");
  }
  if (runtimeUrl != null && command !== "tui") {
    throw new Error("--runtime-url is only valid with tui");
  }

  return { command, reconfigure, nonInteractive, runtimeUrl };
}

export function deploymentHelp() {
  return `./deploy.sh [deploy] [--reconfigure | --non-interactive]
./deploy.sh start|stop|restart|status|logs|doctor|tui|help
./deploy.sh tui [--runtime-url <url>]

deploy   Configure, install, build (including TUI), start, and verify. Existing deployments use a maintenance window.
start    Start an existing build. Does not install or build.
stop     Stop only the managed DataFoundry process group. Keeps configuration and data.
restart  Stop and start an existing build. Does not install or build.
status   Read process state and probe actual service health.
logs     Show recent runtime logs and continue following them. Ctrl+C does not stop DataFoundry.
doctor   Run read-only dependency, configuration, port, permission, disk, and health checks.
tui      Optional: start the TUI client in the foreground after deploy (not a managed background service).
help     Show this help.

One-click deploy builds the TUI so it is ready. Start it in another terminal with ./deploy.sh tui
(or npm run start:tui). It does not stay running with the Web/API stack.
`;
}
