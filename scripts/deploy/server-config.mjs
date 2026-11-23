import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync, renameSync, rmSync, openSync, closeSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { resolveHome } from "../../ui/shared/server-listen.mjs";
import { readConfigYaml, parseConfigYaml, configurationExists } from "../../ui/shared/config-yaml.mjs";

const HOME_CONFIG_TEMPLATE = fileURLToPath(
  new URL("../../dataagent/bootstrap/templates/config.yaml", import.meta.url),
);

/** Edit only server fields and atomically replace with a private same-directory file. */
export function writeServerListen(env, listen) {
  const file = join(resolveHome(env), 'config.yaml');
  const { doc } = readConfigYaml(configurationExists(file) ? file : HOME_CONFIG_TEMPLATE);
  doc.setIn(['server', 'host'], listen.host);
  doc.setIn(['server', 'port'], Number(listen.port));
  const text = doc.toString();
  parseConfigYaml(text, file);
  mkdirSync(dirname(file), { recursive: true });
  const temporary = `${file}.${randomUUID()}.tmp`;
  let created = false;
  try {
    const descriptor = openSync(temporary, 'wx', 0o600);
    created = true;
    try { writeFileSync(descriptor, text); }
    finally { closeSync(descriptor); }
    renameSync(temporary, file);
  } finally {
    if (created) rmSync(temporary, { force: true });
  }
}
