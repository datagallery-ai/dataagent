import { useCallback, useEffect, useRef, useState } from 'react';
import { useApp, useInput, useStdout } from 'ink';

/** Keep V2 job-control behavior local; Ink owns raw mode and the redraw. */
export function useV2TerminalLifecycle(enabled: boolean) {
  const { suspendTerminal, exit } = useApp();
  const { stdout } = useStdout();
  const suspending = useRef(false);
  const [suspended, setSuspended] = useState(false);
  const suspend = useCallback(() => {
    if (!enabled || process.platform === 'win32' || suspending.current) return;
    suspending.current = true;
    setSuspended(true);
    void suspendTerminal(async () => {
      // The shared terminal-screen helper owns the alternate screen and mouse.
      stdout.write('\x1b[?1006l\x1b[?1002l\x1b[?25h\x1b[?1049l');
      await new Promise<void>((resolve) => {
        process.once('SIGCONT', resolve);
        // Suspend the foreground TUI job; its detached backend is not a terminal owner.
        process.kill(0, 'SIGSTOP');
      });
      stdout.write('\x1b[?1049h\x1b[2J\x1b[H\x1b[?25l\x1b[?1002h\x1b[?1006h');
      suspending.current = false;
      setSuspended(false);
      stdout.emit('resize');
    }).catch(exit).finally(() => { suspending.current = false; setSuspended(false); });
  }, [enabled, suspendTerminal, exit, stdout]);

  useEffect(() => {
    if (!enabled || process.platform === 'win32') return;
    process.on('SIGTSTP', suspend);
    return () => { process.removeListener('SIGTSTP', suspend); };
  }, [enabled, suspend]);
  useInput((input, key) => {
    if (key.ctrl && input === 'z') suspend();
  }, { isActive: enabled });
  return { suspended, suspending };
}
