import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { parseConfigYaml } from './config-yaml.mjs';
const cases = JSON.parse(readFileSync(new URL('../../tests/fixtures/yaml-contract.json', import.meta.url), 'utf8'));
for (const item of cases) test(`shared YAML contract: ${item.name}`, () => {
  if (item.reject) {
    assert.throws(() => parseConfigYaml(item.text, 'fixture.yaml'), error => {
      assert.match(error.message, /fixture.yaml/);
      assert.doesNotMatch(error.message, /secret/);
      return true;
    });
  } else assert.deepEqual(parseConfigYaml(item.text).value, item.value);
});
