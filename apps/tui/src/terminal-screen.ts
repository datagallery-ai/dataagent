const ENTER_ALTERNATE_SCREEN = "\u001B[?1049h\u001B[H";
const LEAVE_ALTERNATE_SCREEN = "\u001B[?1049l";
const CLEAR_SCREEN = "\u001B[2J\u001B[H";
const HIDE_CURSOR = "\u001B[?25l";
const ENABLE_MOUSE_TRACKING = "\u001B[?1002h\u001B[?1006h";
const DISABLE_MOUSE_TRACKING = "\u001B[?1006l\u001B[?1002l";
const SHOW_CURSOR = "\u001B[?25h";

export type TerminalScreenOutput = Pick<NodeJS.WriteStream, "write"> & {
  isTTY?: boolean;
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

  let active = true;
  output.write(ENTER_ALTERNATE_SCREEN + CLEAR_SCREEN + HIDE_CURSOR + ENABLE_MOUSE_TRACKING);

  const writeLeaveSequence = () => {
    if (!active) return;
    active = false;
    output.write(DISABLE_MOUSE_TRACKING + SHOW_CURSOR + LEAVE_ALTERNATE_SCREEN);
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
