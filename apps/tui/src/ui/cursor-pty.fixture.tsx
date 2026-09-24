// Real-PTY fixture: no network, no user configuration or session database.
import React, { useLayoutEffect, useState } from 'react';
import { Box, Text, render, useApp, useInput, useStdout } from 'ink';
import { EnhancedInputBox } from './components/EnhancedInputBox.js';
import { useTerminalSize } from './use-terminal-size.js';
import { installFullscreenCursorCorrection } from './fullscreen-cursor.js';
import { installTerminalRedrawOptimizer } from '../terminal-redraw-optimizer.js';
import { withAlternateScreen } from '../terminal-screen.js';

function Fixture() {
  const { rows, columns } = useTerminalSize();
  const { stdout } = useStdout();
  const [generation, setGeneration] = useState(0);
  const [fail, setFail] = useState(false);
  const { exit } = useApp();
  useInput((input, key) => {
    if (key.ctrl && input === 'c') exit();
    if (key.ctrl && input === 'r') setGeneration((value) => value + 1);
    if (key.ctrl && input === 'x') setFail(true);
  });
  if (fail) throw new Error('Intentional cursor fixture failure');
  return <Box height={rows} width={columns} flexDirection="column">
    <Box flexGrow={1}><Text>Cursor PTY fixture {generation}</Text></Box>
    <Composer key={generation} columns={columns} stdout={stdout} />
  </Box>;
}
function Composer({ columns, stdout }: { columns: number; stdout: NodeJS.WriteStream }) {
  useLayoutEffect(() => process.argv.includes('--unpatched') ? undefined : installFullscreenCursorCorrection(stdout), [stdout]);
  return <EnhancedInputBox inputWidth={columns} modelName="test-model" onChange={() => {}} onSubmit={() => {}} />;
}
const originalWrite = process.stdout.write;
const restoreOptimizer = installTerminalRedrawOptimizer(process.stdout);
try {
  await withAlternateScreen(async () => {
    const instance = render(<Fixture />, {
      exitOnCtrlC: false, patchConsole: false, kittyKeyboard: { mode: 'enabled' },
      incrementalRendering: !process.argv.includes('--standard'),
    });
    await instance.waitUntilExit();
  });
} finally {
  restoreOptimizer();
  process.stdout.write(process.stdout.write === originalWrite ? 'WRITER_RESTORED\n' : 'WRITER_LEAK\n');
}
