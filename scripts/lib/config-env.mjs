/** Ordered python-dotenv-compatible bindings and interpolation for server configuration. */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { configurationExists } from './config-yaml.mjs';

function* bindings(source, file) {
  const text = source.replace(/^\uFEFF/u, '');
  let offset = 0;
  function take(pattern) {
    pattern.lastIndex = offset;
    const match = pattern.exec(text);
    if (!match) {
      const line = text.slice(0, offset).split(/\r\n|\r|\n/u).length;
      throw new Error(`${file}: invalid environment file at line ${line}`);
    }
    offset = pattern.lastIndex;
    return match;
  }
  const escapes = { '\\': '\\', "'": "'", '"': '"', a: '\x07', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t', v: '\v' };
  while (offset < text.length) {
    take(/\s*/uy);
    if (offset === text.length) break;
    take(/(?:export[^\S\r\n]+)?/uy);
    let key = null;
    if (text[offset] !== '#') {
      key = text[offset] === "'" ? take(/'([^']+)'/uy)[1] : take(/([^=#\s]+)/uy)[1];
    }
    take(/[^\S\r\n]*/uy);
    let value = null;
    if (text[offset] === '=') {
      take(/=[^\S\r\n]*/uy);
      if (text[offset] === "'") {
        value = take(/'((?:\\[\s\S]|[^'\\])*)'/uy)[1].replace(/\\([\\'])/gu, (_, c) => escapes[c]);
      } else if (text[offset] === '"') {
        value = take(/"((?:\\[\s\S]|[^"\\])*)"/uy)[1].replace(/\\([\\'"abfnrtv])/gu, (_, c) => escapes[c]);
      } else {
        value = take(/[^\r\n]*/uy)[0].replace(/\s+#.*/u, '').trimEnd();
      }
    }
    take(/(?:[^\S\r\n]*#[^\r\n]*)?/uy);
    take(/[^\S\r\n]*(?:\r\n|\r|\n|$)/uy);
    if (key !== null) yield [key, value];
  }
}

export function effectiveEnvironment(env, home, envFile) {
  const file = envFile ?? join(home, '.env');
  const parsed = new Map();
  if (configurationExists(file)) {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(readFileSync(file));
    for (const [key, raw] of bindings(text, file)) {
      // dotenv_values(override=True): earlier bindings override process values during
      // interpolation, including empty/null and reserved bindings. Filter only afterward.
      const value = raw === null ? null : raw.replace(/\$\{([^}:]*)(?::-([^}]*))?\}/gu,
        (_, name, fallback) => (parsed.has(name) ? parsed.get(name) : Object.hasOwn(env, name) ? env[name] : fallback) ?? '');
      parsed.set(key, value);
    }
  } else if (envFile !== undefined) {
    throw new Error(`${file}: environment file does not exist`);
  }
  const values = Object.create(null);
  for (const [key, value] of parsed) {
    if (value && !['DATAAGENT_HOME', 'DATAAGENT_INSTANCE_ID'].includes(key)) values[key] = value;
  }
  for (const [key, value] of Object.entries(env)) if (value) values[key] = value;
  return values;
}
