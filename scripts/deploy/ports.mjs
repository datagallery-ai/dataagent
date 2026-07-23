import { execFile } from "node:child_process";
import net from "node:net";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

export function parsePort(value, label) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`${label} port must be an integer between 1 and 65535`);
  }
  return parsed;
}

export async function describePortOwner(port) {
  if (process.platform !== "linux") return "unknown process";
  try {
    const { stdout } = await execFileAsync("ss", ["-ltnp"], {
      encoding: "utf8",
      maxBuffer: 2 * 1024 * 1024
    });
    const needle = `:${port}`;
    for (const line of stdout.split(/\r?\n/u)) {
      if (!line.includes(needle) || !/\bLISTEN\b/u.test(line)) continue;
      const users = /users:\(\("([^"]+)",pid=(\d+)/u.exec(line);
      if (users) return `${users[1]} pid=${users[2]}`;
      return "unknown process";
    }
  } catch {
    // best effort
  }
  return "unknown process";
}

export async function probePort(host, port, options = {}) {
  const describe = options.describeOwner ?? describePortOwner;
  const available = await new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.listen({ host, port, exclusive: true }, () => {
      server.close(() => resolve(true));
    });
  });
  if (available) return { available: true, owner: null };
  return { available: false, owner: await describe(port) };
}

function defaultPrint(message) {
  process.stdout.write(`${message}\n`);
}

export async function selectDeploymentPort(options) {
  const {
    label,
    defaultPort,
    reserved = new Set(),
    managedPorts = new Set(),
    nonInteractive = false,
    ask,
    probe = (port) => probePort("0.0.0.0", port),
    print = defaultPrint
  } = options;

  let candidate = parsePort(defaultPort, label);
  let explicitChoice = false;

  while (true) {
    if (reserved.has(candidate)) {
      const message = `${label} port ${candidate} is already selected for another service`;
      if (nonInteractive) throw new Error(message);
      print(message);
      const answer = String(await ask(`请为 ${label} 输入其他端口，或输入 q 退出：`)).trim();
      if (answer.toLowerCase() === "q") throw new Error("port selection cancelled");
      candidate = parsePort(answer, label);
      explicitChoice = true;
      continue;
    }

    const result = await probe(candidate);
    const managed = managedPorts.has(candidate);

    if (result.available || managed) {
      if (nonInteractive || explicitChoice) return candidate;

      print(`端口 ${candidate} 当前可用，请选择：`);
      print(`1. 使用端口 ${candidate}`);
      print("2. 指定其他端口");
      const choice = String((await ask("请选择 [1]：")) ?? "").trim() || "1";
      if (choice === "1") return candidate;
      if (choice === "2") {
        const answer = String(await ask(`请输入 ${label} 端口：`)).trim();
        candidate = parsePort(answer, label);
        explicitChoice = true;
        continue;
      }
      print("请输入 1 或 2");
      continue;
    }

    if (nonInteractive) {
      throw new Error(`${label} port ${candidate} is already in use`);
    }

    const owner = result.owner || "unknown process";
    print(`端口 ${candidate} 已被未知进程占用（${owner}）。DataFoundry 不会结束该进程。`);
    const answer = String(await ask("请输入其他端口，或输入 q 退出：")).trim();
    if (answer.toLowerCase() === "q") throw new Error("port selection cancelled");
    candidate = parsePort(answer, label);
    explicitChoice = true;
  }
}

export async function verifySelectedPorts(services, options = {}) {
  const {
    managedPorts = new Set(),
    probe = (port) => probePort("0.0.0.0", port)
  } = options;

  for (const service of services) {
    const port = parsePort(service.port, service.label);
    if (managedPorts.has(port)) continue;
    const result = await probe(port);
    if (!result.available) {
      throw new Error(`${service.label} port ${port} is already in use`);
    }
  }
}
