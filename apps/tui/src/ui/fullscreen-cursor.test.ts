import assert from 'node:assert/strict';
import { it } from 'node:test';
import { PassThrough } from 'node:stream';
import { correctFullscreenCursor, installFullscreenCursorCorrection } from './fullscreen-cursor.js';
import { installTerminalRedrawOptimizer } from '../terminal-redraw-optimizer.js';

const suffix = '\x1b[4A\x1b[6G\x1b[?25h';
it('corrects only the full-height cursor suffix, including cursor-only updates', () => {
  assert.equal(correctFullscreenCursor(suffix), '\x1b[3A\x1b[6G\x1b[?25h');
  assert.equal(correctFullscreenCursor('frame\n' + suffix), 'frame\n\x1b[3A\x1b[6G\x1b[?25h');
  assert.equal(correctFullscreenCursor('\x1b[1A\x1b[1G\x1b[?25h'), '\x1b[1G\x1b[?25h');
  for (const sequence of ['text', '\x1b[4A', suffix + '\n', '\x1b[4B\x1b[G', '\x1b[?25l']) {
    assert.equal(correctFullscreenCursor(sequence), sequence);
  }
});

it('restores in reverse order with redraw optimizer, suspends, and never removes a later writer', () => {
  const output = Object.assign(new PassThrough(), { isTTY: true });
  const stdout = output as unknown as NodeJS.WriteStream;
  const chunks: string[] = [];
  output.on('data', (chunk) => chunks.push(String(chunk)));
  const original = stdout.write;
  const oldSetting = process.env.DATAFOUNDRY_TUI_OPTIMIZE_ERASE_LINES;
  process.env.DATAFOUNDRY_TUI_OPTIMIZE_ERASE_LINES = '1';
  try {
    for (let i = 0; i < 3; i++) {
      const restoreOptimizer = installTerminalRedrawOptimizer(stdout);
      const optimizer = stdout.write;
      let enabled = true;
      const restore = installFullscreenCursorCorrection(stdout, () => enabled);
      stdout.write(suffix);
      assert.equal(chunks.at(-1), correctFullscreenCursor(suffix));
      enabled = false;
      stdout.write(suffix);
      assert.equal(chunks.at(-1), suffix);
      enabled = true;
      stdout.write(Buffer.from(suffix));
      assert.equal(chunks.at(-1), suffix, 'buffers are not rewritten');
      restore();
      assert.equal(stdout.write, optimizer);
      restoreOptimizer();
      assert.equal(stdout.write, original);
    }
    const restore = installFullscreenCursorCorrection(stdout);
    const installed = stdout.write;
    const later = function (chunk: string) { return installed.call(stdout, chunk); } as typeof stdout.write;
    stdout.write = later;
    restore();
    assert.equal(stdout.write, later);
    stdout.write(suffix);
    assert.equal(chunks.at(-1), suffix, 'a removed patch is inert even inside another writer');
  } finally {
    stdout.write = original;
    if (oldSetting === undefined) delete process.env.DATAFOUNDRY_TUI_OPTIMIZE_ERASE_LINES;
    else process.env.DATAFOUNDRY_TUI_OPTIMIZE_ERASE_LINES = oldSetting;
  }
});

it('does not install on non-TTY output', () => {
  const stdout = new PassThrough() as unknown as NodeJS.WriteStream;
  const original = stdout.write;
  installFullscreenCursorCorrection(stdout)();
  assert.equal(stdout.write, original);
});
