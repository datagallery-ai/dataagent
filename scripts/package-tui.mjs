import { spawnSync } from "node:child_process";
import { cp, mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { basename, join, posix } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const dist = join(root, "dist");
const readManifest = async file => JSON.parse(await readFile(join(root, file), "utf8"));
const tui = await readManifest("ui/tui/package.json");
const shared = await readManifest("ui/package.json");

await mkdir(dist, { recursive: true });
const staging = await mkdtemp(join(dist, ".tui-"));
try {
  const packageRoot = join(staging, "package");
  const archive = join(staging, "archive");
  await mkdir(packageRoot);
  await mkdir(archive);
  const filter = source => !basename(source).includes(".test.");
  for (const folder of ["ui/tui/dist", "ui/shared"]) {
    // Preserve the shared imports relative to the compiled TUI.
    await cp(join(root, folder), join(packageRoot, folder), { recursive: true, filter });
  }
  await writeFile(join(packageRoot, "package.json"), JSON.stringify({
    name: tui.name,
    version: tui.version,
    description: tui.description,
    license: tui.license,
    type: tui.type,
    engines: tui.engines,
    dependencies: { ...shared.dependencies, ...tui.dependencies },
    bin: Object.fromEntries(Object.entries(tui.bin).map(([name, entry]) => [name, posix.join("ui/tui", entry)])),
  }, null, 2) + "\n");
  await cp(join(root, "LICENSE"), join(packageRoot, "LICENSE"));
  const pack = spawnSync("npm", ["pack", "--pack-destination", "../archive"], {
    cwd: packageRoot,
    stdio: "inherit",
    shell: process.platform === "win32",
  });
  if (pack.error) throw pack.error;
  if (pack.status !== 0) throw new Error("npm pack failed for the TUI");
  const destination = join(dist, "tui");
  await rm(destination, { recursive: true, force: true });
  await rename(archive, destination);
} finally {
  await rm(staging, { recursive: true, force: true });
}
