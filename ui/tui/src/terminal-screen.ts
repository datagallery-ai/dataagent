const ENTER_ALTERNATE_SCREEN = "\u001B[?1049h\u001B[H";
const LEAVE_ALTERNATE_SCREEN = "\u001B[?1049l";
const CLEAR_SCREEN = "\u001B[2J\u001B[H";
const HIDE_CURSOR = "\u001B[?25l";
// 1000 = basic button/wheel, 1002 = drag motion, 1006 = SGR coordinates.
const ENABLE_MOUSE_TRACKING = "\u001B[?1000h\u001B[?1002h\u001B[?1006h";
const DISABLE_MOUSE_TRACKING = "\u001B[?1006l\u001B[?1002l\u001B[?1000l";
const SHOW_CURSOR = "\u001B[?25h";

/** Re-assert mouse modes (Ink/ConPTY may clear them after raw-mode or resize). */
export function enableMouseTracking(output: TerminalScreenOutput = process.stdout): void {
  if (!output.isTTY) return;
  try {
    output.write(ENABLE_MOUSE_TRACKING);
  } catch {
    // Best-effort.
  }
}

export function disableMouseTracking(output: TerminalScreenOutput = process.stdout): void {
  if (!output.isTTY) return;
  try {
    output.write(DISABLE_MOUSE_TRACKING);
  } catch {
    // Best-effort.
  }
}

/**
 * Cursor / Windows ConPTY has crashed on alternate-screen and mouse/kitty
 * teardown ("terminal闪退"). Keep the Windows path free of those sequences.
 */
const windowsConPty = process.platform === "win32";

/**
 * Set when {@link enterAlternateScreen} arms a real TTY session. Cleared by
 * {@link restoreTerminalForParentShell} so --help / arg-error paths that never
 * started Ink do not resume stdin and keep the event loop alive.
 */
let ttySessionArmed = false;

export type TerminalScreenOutput = Pick<NodeJS.WriteStream, "write"> & {
  isTTY?: boolean;
};

export type TerminalScreenInput = {
  isTTY?: boolean;
  isRaw?: boolean;
  setRawMode?: (mode: boolean) => void;
  resume(): void;
  pause(): void;
  read(): string | Buffer | null;
};

export type TerminalScreenSession = {
  readonly enabled: boolean;
  leave(): void;
};

export function enterAlternateScreen(
  output: TerminalScreenOutput = process.stdout,
): TerminalScreenSession {
  if (!output.isTTY) {
    return {
      enabled: false,
      leave: () => {},
    };
  }

  ttySessionArmed = true;
  let active = true;
  // Windows ConPTY: alternate screen teardown flash-closes Cursor tabs. Keep the
  // primary buffer, but still enable mouse tracking so wheel scroll reaches Ink.
  const enter = windowsConPty
    ? ENABLE_MOUSE_TRACKING
    : ENTER_ALTERNATE_SCREEN + CLEAR_SCREEN + HIDE_CURSOR + ENABLE_MOUSE_TRACKING;
  output.write(enter);

  const writeLeaveSequence = () => {
    if (!active) return;
    active = false;
    if (windowsConPty) {
      disableMouseTracking(output);
    } else {
      output.write(DISABLE_MOUSE_TRACKING + SHOW_CURSOR + LEAVE_ALTERNATE_SCREEN);
    }
  };

  process.once("exit", writeLeaveSequence);

  return {
    enabled: true,
    leave: () => {
      process.removeListener("exit", writeLeaveSequence);
      writeLeaveSequence();
    },
  };
}

export async function withAlternateScreen<T>(
  operation: () => Promise<T>,
  output: TerminalScreenOutput = process.stdout,
): Promise<T> {
  const screen = enterAlternateScreen(output);
  try {
    return await operation();
  } finally {
    screen.leave();
  }
}

/**
 * Restore mouse tracking / cursor, leave raw mode, and discard unread stdin so
 * PowerShell does not see a leftover `exit`. Avoid alternate-screen CSI on Windows.
 *
 * Only drains stdin when a TTY session was armed (or raw mode is still on).
 * Always ends paused so a stray resume cannot keep the process alive after
 * --help / argument-error paths that never started Ink.
 */
export function restoreTerminalForParentShell(
  stdin: TerminalScreenInput = process.stdin,
  stdout: TerminalScreenOutput = process.stdout,
): void {
  const armed = ttySessionArmed;
  ttySessionArmed = false;

  if (stdout.isTTY && armed) {
    try {
      if (windowsConPty) {
        disableMouseTracking(stdout);
      } else {
        stdout.write(DISABLE_MOUSE_TRACKING + SHOW_CURSOR);
      }
    } catch {
      // Best-effort restore.
    }
  }
  if (!stdin.isTTY) return;

  let wasRaw = false;
  try {
    wasRaw = Boolean(stdin.isRaw);
    if (typeof stdin.setRawMode === "function" && wasRaw) {
      stdin.setRawMode(false);
    }
  } catch {
    // Ignore terminals that already left raw mode.
  }

  try {
    if (armed || wasRaw) {
      stdin.resume();
      while (stdin.read() !== null) {
        // Discard queued keystrokes so they cannot reach the parent shell.
      }
    }
  } catch {
    // Ignore closed stdin during teardown.
  } finally {
    try {
      stdin.pause();
    } catch {
      // Ignore closed stdin during teardown.
    }
  }
}
