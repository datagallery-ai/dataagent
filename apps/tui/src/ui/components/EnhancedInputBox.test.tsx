import React from 'react';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { afterEach, describe, it } from 'node:test';
import chalk from 'chalk';
import { Box, render } from 'ink';
import { CommandHistory } from '../keybindings.js';
import { OutputsScreen } from '../OutputsView.js';
import { ResourcePicker } from '../ResourcePicker.js';
import { SessionPicker } from '../SessionPicker.js';
import { SlashCommandPopover } from '../SlashCommandPopover.js';
import { StatusBar } from '../StatusBar.js';
import { selectionColors } from '../theme.js';
import { themeManager } from '../themes/theme-manager.js';
import { EnhancedInputBox, inputLineFragments } from './EnhancedInputBox.js';

const waitForInput = () => new Promise<void>((resolve) => setImmediate(resolve));

type TestInput = PassThrough & {
  isTTY: boolean;
  isRaw: boolean;
  setRawMode: (mode: boolean) => void;
  ref: () => TestInput;
  unref: () => TestInput;
};

type TestOutput = PassThrough & {
  columns: number;
  rows: number;
  isTTY: boolean;
};

const activeViews: Array<ReturnType<typeof render>> = [];

function renderInputBox(element: React.ReactElement) {
  const stdin = new PassThrough() as TestInput;
  stdin.isTTY = true;
  stdin.isRaw = false;
  stdin.setRawMode = (mode) => {
    stdin.isRaw = mode;
  };
  stdin.ref = () => stdin;
  stdin.unref = () => stdin;

  const stdout = new PassThrough() as TestOutput;
  stdout.columns = 100;
  stdout.rows = 40;
  stdout.isTTY = true;
  const stderr = new PassThrough() as TestOutput;
  stderr.columns = 100;
  stderr.rows = 40;
  stderr.isTTY = true;
  const output: string[] = [];
  stdout.on('data', (chunk: Buffer) => output.push(chunk.toString()));

  const view = render(element, {
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    debug: true,
    exitOnCtrlC: false,
    patchConsole: false,
    interactive: true,
  });
  activeViews.push(view);
  return { ...view, stdin, output };
}

afterEach(() => {
  for (const view of activeViews.splice(0)) {
    view.unmount();
    view.cleanup();
  }
});

describe('EnhancedInputBox paste handling', () => {
  it('folds a paste over 1000 characters and expands it on submit', async () => {
    const changes: string[] = [];
    const submissions: string[] = [];
    const largePaste = 'x'.repeat(1001);
    const view = renderInputBox(
      <EnhancedInputBox
        commands={[]}
        onChange={(value) => changes.push(value)}
        onSubmit={(value) => submissions.push(value)}
      />,
    );
    await waitForInput();

    view.stdin.write(`\x1b[200~${largePaste}\x1b[201~`);
    await waitForInput();

    assert.equal(changes.at(-1), '[Pasted Content 1001 chars]');
    assert.match(view.output.join(''), /\[Pasted Content 1001 chars\]/);
    assert.deepEqual(
      inputLineFragments('before [Pasted Content 1001 chars] after', null),
      [
        { text: 'before ', isPastePlaceholder: false, hasCursor: false },
        {
          text: '[Pasted Content 1001 chars]',
          isPastePlaceholder: true,
          hasCursor: false,
        },
        { text: ' after', isPastePlaceholder: false, hasCursor: false },
      ],
    );

    view.stdin.write('\r');
    await waitForInput();

    assert.deepEqual(submissions, [largePaste]);
  });

  it('folds a paste with more than 10 lines', async () => {
    const changes: string[] = [];
    const multiLinePaste = Array.from({ length: 11 }, () => 'line').join('\n');
    const view = renderInputBox(
      <EnhancedInputBox
        commands={[]}
        onChange={(value) => changes.push(value)}
        onSubmit={() => {}}
      />,
    );
    await waitForInput();

    view.stdin.write(`\x1b[200~${multiLinePaste}\x1b[201~`);
    await waitForInput();

    assert.equal(
      changes.at(-1),
      `[Pasted Content ${multiLinePaste.length} chars]`,
    );
  });

  it('keeps a small multiline paste editable in the buffer', async () => {
    const changes: string[] = [];
    const view = renderInputBox(
      <EnhancedInputBox
        commands={[]}
        onChange={(value) => changes.push(value)}
        onSubmit={() => {}}
      />,
    );
    await waitForInput();

    view.stdin.write('\x1b[200~first\nsecond\x1b[201~');
    await waitForInput();

    assert.equal(changes.at(-1), 'first\nsecond');
  });
});

describe('EnhancedInputBox history navigation', () => {
  it('moves to the first column before restoring older history', async () => {
    const history = new CommandHistory();
    history.add('previous command');
    const changes: string[] = [];
    const view = renderInputBox(
      <EnhancedInputBox
        commands={[]}
        history={history}
        onChange={(value) => changes.push(value)}
        onSubmit={() => {}}
      />,
    );
    await waitForInput();

    view.stdin.write('draft');
    await waitForInput();
    const callsAfterTyping = changes.length;

    view.stdin.write('\x1b[A');
    await waitForInput();

    assert.equal(changes.at(-1), 'draft');
    assert.equal(changes.length, callsAfterTyping);

    view.stdin.write('\x1b[A');
    await waitForInput();

    assert.equal(changes.at(-1), 'previous command');

    view.stdin.write('X');
    await waitForInput();

    assert.equal(changes.at(-1), 'Xprevious command');
  });

  it('moves to the last column before restoring the original draft', async () => {
    const history = new CommandHistory();
    history.add('previous command');
    const changes: string[] = [];
    const view = renderInputBox(
      <EnhancedInputBox
        commands={[]}
        history={history}
        onChange={(value) => changes.push(value)}
        onSubmit={() => {}}
      />,
    );
    await waitForInput();

    view.stdin.write('draft');
    await waitForInput();
    view.stdin.write('\x1b[A');
    await waitForInput();
    view.stdin.write('\x1b[A');
    await waitForInput();
    const callsAfterHistoryRestore = changes.length;

    view.stdin.write('\x1b[B');
    await waitForInput();

    assert.equal(changes.at(-1), 'previous command');
    assert.equal(changes.length, callsAfterHistoryRestore);

    view.stdin.write('\x1b[B');
    await waitForInput();

    assert.equal(changes.at(-1), 'draft');
  });

  it('retains submitted history when the composer is remounted', async () => {
    const history = new CommandHistory();
    const firstSubmissions: string[] = [];
    const firstView = renderInputBox(
      <EnhancedInputBox
        commands={[]}
        history={history}
        onChange={() => {}}
        onSubmit={(value) => firstSubmissions.push(value)}
      />,
    );
    await waitForInput();

    firstView.stdin.write('remember me');
    await waitForInput();
    firstView.stdin.write('\r');
    await waitForInput();
    assert.deepEqual(firstSubmissions, ['remember me']);
    firstView.unmount();

    const changes: string[] = [];
    const secondView = renderInputBox(
      <EnhancedInputBox
        commands={[]}
        history={history}
        onChange={(value) => changes.push(value)}
        onSubmit={() => {}}
      />,
    );
    await waitForInput();

    secondView.stdin.write('\x1b[A');
    await waitForInput();

    assert.equal(changes.at(-1), 'remember me');
  });
});

describe('EnhancedInputBox slash command menu', () => {
  it('paints a solid surface behind the overlaid command rows', async () => {
    const previousColorLevel = chalk.level;
    chalk.level = 3;
    try {
      const view = renderInputBox(
        <SlashCommandPopover
          activeIndex={0}
          commands={[
            { name: 'reset', description: 'Reset session and start fresh' },
            { name: 'resume', description: 'Resume a server session' },
          ]}
        />,
      );
      await waitForInput();

      const output = view.output.join('');
      assert.match(output, /\u001B\[48;2;17;23;25m/);
      assert.match(output, /\u001B\[48;2;27;39;44m/);
    } finally {
      chalk.level = previousColorLevel;
    }
  });

  it('keeps the composer height fixed while the menu overlays the chat viewport', async () => {
    const layoutRows: number[] = [];
    const view = renderInputBox(
      <Box height={20} width="100%" flexDirection="column" justifyContent="flex-end">
        <EnhancedInputBox
          onChange={() => {}}
          onSubmit={() => {}}
          onLayoutChange={(rows) => layoutRows.push(rows)}
        />
      </Box>,
    );
    await waitForInput();

    assert.equal(layoutRows.at(-1), 9);

    view.stdin.write('/');
    await waitForInput();
    await waitForInput();

    assert.equal(layoutRows.at(-1), 9);
    assert.match(view.output.join(''), /\/clear\s+Clear chat history/);
    assert.doesNotMatch(view.output.join(''), /Slash Commands/);
  });

  it('uses Enter to execute the selected command immediately', async () => {
    const submissions: string[] = [];
    const view = renderInputBox(
      <Box height={20} width="100%" flexDirection="column" justifyContent="flex-end">
        <EnhancedInputBox
          onChange={() => {}}
          onSubmit={(value) => submissions.push(value)}
        />
      </Box>,
    );
    await waitForInput();

    view.stdin.write('/');
    await waitForInput();
    view.stdin.write('\x1b[B');
    await waitForInput();
    view.stdin.write('\r');
    await waitForInput();

    assert.deepEqual(submissions, ['/datasource']);
  });

  it('keeps Tab as completion without executing the selected command', async () => {
    const changes: string[] = [];
    const submissions: string[] = [];
    const view = renderInputBox(
      <Box height={20} width="100%" flexDirection="column" justifyContent="flex-end">
        <EnhancedInputBox
          onChange={(value) => changes.push(value)}
          onSubmit={(value) => submissions.push(value)}
        />
      </Box>,
    );
    await waitForInput();

    view.stdin.write('/');
    await waitForInput();
    view.stdin.write('\x1b[B');
    await waitForInput();
    view.stdin.write('\t');
    await waitForInput();

    assert.equal(changes.at(-1), '/datasource ');
    assert.deepEqual(submissions, []);
  });

  it('submits an unmatched slash command instead of trapping Enter in an empty menu', async () => {
    const submissions: string[] = [];
    const view = renderInputBox(
      <Box height={20} width="100%" flexDirection="column" justifyContent="flex-end">
        <EnhancedInputBox
          onChange={() => {}}
          onSubmit={(value) => submissions.push(value)}
        />
      </Box>,
    );
    await waitForInput();

    view.stdin.write('/no-such-command');
    await waitForInput();
    assert.match(view.output.join(''), /No matching commands/);

    view.stdin.write('\r');
    await waitForInput();

    assert.deepEqual(submissions, ['/no-such-command']);
  });
});

describe('EnhancedInputBox layout', () => {
  it('keeps all three input viewport rows visible inside the full border', async () => {
    const view = renderInputBox(
      <Box width={88}>
        <EnhancedInputBox
          value={'first\nsecond\nthird'}
          inputWidth={88}
          datasourceId="dtc-growth-demo"
          skillId="data-analysis"
          onChange={() => {}}
          onSubmit={() => {}}
        />
      </Box>,
    );
    await waitForInput();
    await waitForInput();

    assert.match(view.output.join(''), /third/);
  });

  it('uses the compact shortcut footer at the 76-column home width', async () => {
    const view = renderInputBox(
      <Box width={76}>
        <EnhancedInputBox
          inputWidth={76}
          datasourceId="dtc-growth-demo"
          skillId="data-analysis"
          onChange={() => {}}
          onSubmit={() => {}}
        />
      </Box>,
    );
    await waitForInput();

    const output = view.output.join('');
    assert.match(output, /ANALYZE/);
    assert.match(output, /dtc-growth-demo/);
    assert.match(output, /\[Enter\]/);
    assert.doesNotMatch(output, /\[Shift\+Enter\]/);
  });

  it('intercepts configured home shortcuts before inserting text', async () => {
    const changes: string[] = [];
    const shortcuts: string[] = [];
    const view = renderInputBox(
      <EnhancedInputBox
        onChange={(nextValue) => changes.push(nextValue)}
        onSubmit={() => {}}
        onShortcut={(input) => {
          shortcuts.push(input);
          return input === '1';
        }}
      />,
    );
    await waitForInput();

    view.stdin.write('1');
    await waitForInput();

    assert.deepEqual(shortcuts, ['1']);
    assert.deepEqual(changes, []);
  });
});

describe('StatusBar', () => {
  const startup = {
    threadId: 'thread-1',
    connectionStatus: 'connected',
    runStatus: 'running',
    modelName: 'Qwen3-32B',
    directory: '/tmp',
    datasourceId: 'dtc-growth-demo',
  } as const;

  it('shows live run, datasource, and model state when space is available', async () => {
    const view = renderInputBox(
      <Box width={80}>
        <StatusBar columns={80} startup={startup} />
      </Box>,
    );
    await waitForInput();

    const output = view.output.join('');
    assert.match(output, /Running/);
    assert.match(output, /source: /);
    assert.match(output, /dtc-growth-demo/);
    assert.match(output, /model: /);
    assert.match(output, /Qwen3-32B/);
  });

  it('keeps only the primary state on narrow terminals', async () => {
    const view = renderInputBox(
      <Box width={39}>
        <StatusBar columns={39} startup={startup} />
      </Box>,
    );
    await waitForInput();

    const output = view.output.join('');
    assert.match(output, /Running/);
    assert.doesNotMatch(output, /source: /);
    assert.doesNotMatch(output, /model: /);
  });
});

describe('shared selection theme', () => {
  it('uses the same mist palette for session, resource, and output selection screens', async () => {
    const previousColorLevel = chalk.level;
    chalk.level = 3;
    try {
      const sessionView = renderInputBox(
        <SessionPicker
          sessions={[
            {
              id: 'session-1',
              threadId: 'thread-1',
              title: 'Revenue analysis',
              updatedAt: new Date().toISOString(),
            },
          ]}
          loading={false}
          columns={72}
          rows={20}
          onSelect={() => {}}
          onCancel={() => {}}
        />,
      );
      const resourceView = renderInputBox(
        <ResourcePicker
          title="Select a data source"
          items={[
            {
              id: 'dtc-growth-demo',
              name: 'DTC Growth Demo',
              description: 'Built-in analytics data source',
              enabled: true,
            },
          ]}
          loading={false}
          columns={72}
          rows={20}
          emptyMessage="No data sources configured."
          onSelect={() => {}}
          onCancel={() => {}}
        />,
      );
      const outputsView = renderInputBox(
        <OutputsScreen
          artifacts={[
            {
              id: 'artifact-1',
              title: 'Revenue by channel',
              kind: 'csv',
              type: 'dataset',
              summary: 'Monthly revenue grouped by channel',
            },
          ]}
          events={[]}
          columns={72}
          rows={20}
          onCancel={() => {}}
        />,
      );
      await waitForInput();
      await waitForInput();

      for (const output of [
        sessionView.output.join(''),
        resourceView.output.join(''),
        outputsView.output.join(''),
      ]) {
        assert.match(output, /\u001B\[48;2;17;23;25m/);
        assert.match(output, /\u001B\[48;2;27;39;44m/);
        assert.match(output, /\u001B\[38;2;121;165;169m/);
      }
    } finally {
      chalk.level = previousColorLevel;
    }
  });

  it('switches all semantic colors through one preset manager', () => {
    assert.equal(themeManager.setActiveTheme('legacy'), true);
    assert.equal(selectionColors.accent, '#6CA8E8');
    assert.equal(selectionColors.background, '#121820');

    assert.equal(themeManager.setActiveTheme('mist-dark'), true);
    assert.equal(selectionColors.accent, '#79A5A9');
    assert.equal(selectionColors.background, '#111719');
    assert.equal(themeManager.setActiveTheme('unknown-theme'), false);
  });
});
