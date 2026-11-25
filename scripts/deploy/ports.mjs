import { execFile } from "node:child_process";
import dns from "node:dns/promises";
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

function normalizeAddress(host) {
  const bare = host.replace(/^\[(.*)\]$/u, "$1");
  if (bare === "*") return "::";
  if (net.isIP(bare) !== 6) return bare;
  return new net.SocketAddress({ address: bare, family: "ipv6" }).address
    .replace(/^::ffff:(?=\d+\.)/u, "");
}

async function addressesFor({ host, allAddresses = false }) {
  const address = normalizeAddress(host || "0.0.0.0");
  if (net.isIP(address)) return [address];
  // Node listens on the first DNS result; Python's hostname bind uses all results.
  if (!allAddresses) return [normalizeAddress((await dns.lookup(address)).address)];
  return [...new Set((await dns.lookup(address, { all: true })).map(entry => normalizeAddress(entry.address)))];
}

async function bindingsOverlap(left, right) {
  if (Number(left.port) !== Number(right.port)) return false;
  const [leftAddresses, rightAddresses] = await Promise.all([
    addressesFor(left), addressesFor(right),
  ]);
  return leftAddresses.some(a => rightAddresses.some(b =>
    a === b ||
    (a === "::" && (!(left.ipv6Only ?? left.allAddresses) || net.isIP(b) === 6)) ||
    (b === "::" && (!(right.ipv6Only ?? right.allAddresses) || net.isIP(a) === 6)) ||
    (a === "0.0.0.0" && net.isIP(b) === 4) ||
    (b === "0.0.0.0" && net.isIP(a) === 4)
  ));
}

export async function readPortListeners(port, options = {}) {
  if (process.platform !== "linux" && !options.runSs) return null;
  try {
    const stdout = options.runSs ? await options.runSs() : (
      await execFileAsync("ss", ["-H", "-ltnpe", `sport = :${port}`], { encoding: "utf8" })
    ).stdout;
    return stdout.split(/\r?\n/u).flatMap(line => {
      const [state, , , local] = line.trim().split(/\s+/u, 5);
      const address = /^(.*):(\d+)$/u.exec(local ?? "");
      if (state !== "LISTEN" || !address || Number(address[2]) !== Number(port)) return [];
      const host = address[1].replace(/^\[(.*)\]$/u, "$1").split("%", 1)[0];
      if (host !== "*" && !net.isIP(host)) return [];
      const users = [...line.matchAll(/\("([^"]+)",pid=(\d+),fd=\d+/gu)];
      return [{
        host, port, ipv6Only: /\bv6only:1\b/u.test(line),
        owners: users.map(([, name, pid]) => ({ name, pid: Number(pid) })),
      }];
    });
  } catch {
    return null;
  }
}

export async function probePort(host, port, { allAddresses = false } = {}) {
  if (allAddresses && !net.isIP(normalizeAddress(host))) {
    let addresses;
    try {
      addresses = await addressesFor({ host, allAddresses });
    } catch (error) {
      return { available: false, code: error.code, listeners: null };
    }
    const failures = (await Promise.all(addresses.map(address => probePort(address, port, { allAddresses }))))
      .filter(result => !result.available);
    const error = failures.find(result => result.code !== "EADDRINUSE");
    if (error) return error;
    if (!failures.length) return { available: true, owner: null };
    return {
      ...failures[0],
      listeners: failures.every(result => result.listeners?.length)
        ? failures.flatMap(result => result.listeners) : [],
    };
  }
  const error = await new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", resolve);
    server.listen({ host, port, exclusive: true, ipv6Only: allAddresses }, () => {
      server.close(() => resolve(null));
    });
  });
  if (!error) return { available: true, owner: null };
  const sockets = error.code === "EADDRINUSE" ? await readPortListeners(port) : null;
  const matches = sockets && await Promise.all(sockets.map(socket =>
    bindingsOverlap({ host: error.address || host, port, allAddresses }, socket)
  ));
  const listeners = sockets?.filter((_socket, index) => matches[index]) ?? null;
  const owner = listeners?.[0]?.owners[0];
  return {
    available: false, code: error.code, listeners,
    owner: owner ? `${owner.name} pid=${owner.pid}` : "unknown process",
  };
}

function defaultPrint(message) {
  process.stdout.write(`${message}\n`);
}

export async function selectDeploymentPort(options) {
  const {
    label,
    host = "0.0.0.0",
    allAddresses = false,
    defaultPort,
    reserved = [],
    nonInteractive = false,
    ask,
    probe = (port) => probePort(host, port, { allAddresses }),
    print = defaultPrint
  } = options;

  let candidate = parsePort(defaultPort, label);
  let explicitChoice = false;

  while (true) {
    const conflicts = await Promise.all(reserved.map(binding =>
      bindingsOverlap(binding, { host, port: candidate, allAddresses })
    ));
    if (conflicts.some(Boolean)) {
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
    if (result.available || result.managed) {
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
    print(`端口 ${candidate} 已被未知进程占用（${owner}）。DataGallery 不会结束该进程。`);
    const answer = String(await ask("请输入其他端口，或输入 q 退出：")).trim();
    if (answer.toLowerCase() === "q") throw new Error("port selection cancelled");
    candidate = parsePort(answer, label);
    explicitChoice = true;
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function verifySelectedPorts(services, options = {}) {
  const {
    probe = (port, host, options) => probePort(host, port, options),
    retries = 5,
    retryDelayMs = 200
  } = options;

  const reserved = [];
  for (const service of services) {
    const port = parsePort(service.port, service.label);
    const binding = { host: service.host || "0.0.0.0", port, allAddresses: service.allAddresses };
    const conflicts = await Promise.all(reserved.map(previous => bindingsOverlap(previous, binding)));
    if (conflicts.some(Boolean)) throw new Error(`${service.label} port ${port} is already selected for another service`);
    reserved.push(binding);

    let lastResult = null;
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      lastResult = await probe(port, binding.host, { allAddresses: binding.allAddresses });
      if (lastResult.available || lastResult.managed) break;
      if (attempt < retries) await sleep(retryDelayMs);
    }
    if (!lastResult?.available && !lastResult?.managed) {
      throw new Error(`${service.label} port ${port} is already in use`);
    }
  }
}
