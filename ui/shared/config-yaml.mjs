/** Strict YAML configuration. Diagnostics never include parser source excerpts. */
import { readFileSync, existsSync, lstatSync } from 'node:fs';
import { extname } from 'node:path';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);

export function checkYamlPath(file) {
  if (!['.yaml', '.yml'].includes(extname(file).toLowerCase())) {
    throw new Error(`${file}: YAML extension required`);
  }
}
export function configurationExists(file) {
  try { lstatSync(file); return true; }
  catch (error) {
    if (error.code === 'ENOENT') return false;
    throw new Error(`${file}: cannot inspect configuration path`);
  }
}
export function parseConfigYaml(text, file = '<yaml>') {
  // Deployment help and dependency installation must work before installing frontend dependencies.
  const { parseDocument, isMap, isSeq, isScalar, isAlias, LineCounter } = require('yaml');
  const lines = new LineCounter();
  function fail(category, offset) {
    const pos = typeof offset === 'number' ? lines.linePos(offset) : null;
    throw new Error(`${file}: ${category}${pos ? ` at ${pos.line}:${pos.col}` : ''}`);
  }
  // Sanitize parser failures; bound tree traversal before converting values.
  let doc;
  try {
    doc = parseDocument(text, { version: '1.2', schema: 'core', prettyErrors: false, lineCounter: lines, intAsBigInt: true });
  } catch {
    fail('invalid YAML or resource limit exceeded');
  }
  if (doc.errors.length || doc.warnings.length) fail('invalid YAML syntax', doc.errors[0]?.pos?.[0] ?? doc.warnings[0]?.pos?.[0]);
  if (doc.directives.yaml.version !== '1.2') fail('YAML version must be 1.2');
  function convert(node, depth = 0) {
    const offset = node?.range?.[0];
    if (depth > 64) fail('maximum YAML depth exceeded', offset);
    if (isAlias(node) || node?.anchor) fail('anchors and aliases are forbidden', offset);
    if (node?.tag) fail('explicit tags are forbidden', offset);
    if (isMap(node)) {
      const result = {};
      for (const pair of node.items) {
        if (!isScalar(pair.key) || typeof pair.key.value !== 'string') fail('mapping keys must be strings', pair.key?.range?.[0]);
        convert(pair.key, depth + 1);
        const key = pair.key.value;
        if (key === '<<') fail('merge keys are forbidden', pair.key.range?.[0]);
        if (Object.hasOwn(result, key)) fail('duplicate mapping key', pair.key.range?.[0]);
        Object.defineProperty(result, key, { value: convert(pair.value, depth + 1), enumerable: true, writable: true, configurable: true });
      }
      return result;
    }
    if (isSeq(node)) return node.items.map(item => convert(item, depth + 1));
    const value = node?.value ?? null;
    if (typeof value === 'bigint') {
      if (value > BigInt(Number.MAX_SAFE_INTEGER) || value < -BigInt(Number.MAX_SAFE_INTEGER)) fail('number outside safe range; use a quoted string', offset);
      return Number(value);
    }
    if (typeof value === 'number' && (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)))) fail('number outside safe range; use a quoted string', offset);
    return value;
  }
  if (!isMap(doc.contents)) fail('expected a nonempty mapping document');
  const value = convert(doc.contents);
  return { doc, value };
}
export function readConfigYaml(file) {
  checkYamlPath(file);
  if (!existsSync(file)) throw new Error(`Configuration file does not exist: ${file}`);
  let text;
  try { text = new TextDecoder('utf-8', { fatal: true }).decode(readFileSync(file)); }
  catch { throw new Error(`${file}: cannot read UTF-8 configuration`); }
  return parseConfigYaml(text, file);
}
