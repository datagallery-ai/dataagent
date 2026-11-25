import path from "node:path";

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
  const tokens = argv.some(token => token === "-h" || token === "--help") ? ["help"] : argv;
  let command = "deploy";
  let reconfigure = false;
  let nonInteractive = false;
  let runtimeUrl = null;
  let webHost = null;
  let webPort = null;
  let config = null;
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
    if (token === "--runtime-url" || token === "--web-host" || token === "--web-port" || token === "--config") {
      const value = tokens[i + 1];
      if (!value || value.startsWith("-")) {
        throw new Error(`${token} requires a value`);
      }
      if (token === "--runtime-url") runtimeUrl = value;
      if (token === "--web-host") webHost = value;
      if (token === "--web-port") webPort = value;
      if (token === "--config") {
        const file = value.trim();
        if (!file) throw new Error("--config requires a value");
        config = path.resolve(file);
      }
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
  if ((webHost != null || webPort != null) && !["deploy", "start", "restart", "doctor"].includes(command)) {
    throw new Error("--web-host and --web-port are only valid with deploy, start, restart, or doctor");
  }
  if (config != null && !["deploy", "start", "restart", "doctor"].includes(command)) {
    throw new Error("--config is only valid with deploy, start, restart, or doctor");
  }

  return { command, reconfigure, nonInteractive, runtimeUrl, webHost, webPort, config };
}

export function deploymentHelp() {
  return `./deploy.sh [deploy] [--reconfigure | --non-interactive] [--web-host <host>] [--web-port <port>] [--config <path>]
./deploy.sh start|restart|doctor [--web-host <host>] [--web-port <port>] [--config <path>]
./deploy.sh stop|status|logs|help
./deploy.sh tui [--runtime-url <url>]

deploy   Configure, install, build (including TUI), start, and verify. Existing deployments use a maintenance window.
start    Start an existing build. Does not install or build. Web defaults to 127.0.0.1:3000.
stop     Stop only the managed DataGallery process group. Keeps configuration and data.
restart  Check and reuse the running deployment's settings, then stop and start. Flags override saved settings.
status   Read process state and probe the API address recorded at start.
logs     Show recent runtime logs and continue following them. Ctrl+C does not stop DataGallery.
doctor   Run read-only dependency, configuration, port, permission, disk, and health checks.
tui      Run the foreground client. Use --runtime-url to select an API, or omit it to use the saved address or Home config.
help     Show this help (also -h or --help).

Deploy, restart, and doctor use explicit options first, then a running deployment's saved settings, then defaults.
An explicit --web-port is checked directly without prompting for another Web port.
One-click deploy builds the TUI so it is ready. Start it in another terminal with ./deploy.sh tui.
The deploy.sh tui command connects to an existing API.
npm run start:tui uses Home config.yaml and starts a backend when that address is down.
The TUI does not stay running with the Web/API stack.
`;
}
