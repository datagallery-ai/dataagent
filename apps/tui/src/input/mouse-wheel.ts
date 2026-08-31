function positiveIntegerFromEnv(value: string | undefined, fallback: number): number {
  if (!value) return fallback;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export const WHEEL_LINES_PER_TICK = positiveIntegerFromEnv(
  process.env.DATAFOUNDRY_TUI_WHEEL_LINES_PER_TICK,
  3,
);
const MAX_MOUSE_SEQUENCE_LENGTH = 50;

type ParsedMouseInput = {
  length: number;
  delta: number;
};

export function isMouseInput(input: string): boolean {
  return (
    /\u001B?\[<\d+;\d+;\d+[mM]/.test(input) ||
    /^\u001B?\[<\d+;\d+;\d+[mM]$/.test(input) ||
    /^\u001B?\[M/.test(input)
  );
}

function wheelDeltaFromButtonCode(code: number): number {
  if (!Number.isFinite(code)) return 0;
  if ((code & 64) !== 64) return 0;
  return (code & 1) === 0 ? WHEEL_LINES_PER_TICK : -WHEEL_LINES_PER_TICK;
}

function parseMouseInputAtStart(input: string): ParsedMouseInput | null {
  const sgr = input.match(/^\u001B?\[<(\d+);(\d+);(\d+)([mM])/);
  if (sgr) {
    return {
      length: sgr[0].length,
      delta: wheelDeltaFromButtonCode(Number(sgr[1])),
    };
  }

  const x11 = input.match(/^\u001B?\[M([\s\S]{3})/);
  if (x11) {
    return {
      length: x11[0].length,
      delta: wheelDeltaFromButtonCode(x11[1].charCodeAt(0) - 32),
    };
  }

  return null;
}

function isIncompleteMouseInput(input: string): boolean {
  if (input.length === 0) return false;

  const sgrPrefixes = ['\u001B[<', '[<'];
  if (sgrPrefixes.some((prefix) => prefix.startsWith(input))) {
    return true;
  }
  if (sgrPrefixes.some((prefix) => input.startsWith(prefix))) {
    return !/[mM]/.test(input) && input.length < MAX_MOUSE_SEQUENCE_LENGTH;
  }

  const x11Prefixes = ['\u001B[M', '[M'];
  if (x11Prefixes.some((prefix) => prefix.startsWith(input))) {
    return true;
  }
  if (x11Prefixes.some((prefix) => input.startsWith(prefix))) {
    return input.length < x11Prefixes[0].length + 3;
  }

  return false;
}

export function wheelScrollDeltas(input: string): number[] {
  const deltas: number[] = [];
  let offset = 0;

  while (offset < input.length) {
    const chunk = input.slice(offset);
    const parsed = parseMouseInputAtStart(chunk);
    if (parsed) {
      if (parsed.delta !== 0) {
        deltas.push(parsed.delta);
      }
      offset += parsed.length;
      continue;
    }

    offset += 1;
  }

  return deltas;
}

export function wheelScrollDelta(input: string): number {
  return wheelScrollDeltas(input).reduce((sum, delta) => sum + delta, 0);
}

export type WheelScrollDecoder = {
  push(input: string): number[];
  reset(): void;
};

export function createWheelScrollDecoder(): WheelScrollDecoder {
  let pending = '';

  return {
    push(input: string): number[] {
      let buffer = pending + input;
      pending = '';
      const deltas: number[] = [];

      while (buffer.length > 0) {
        const parsed = parseMouseInputAtStart(buffer);
        if (parsed) {
          if (parsed.delta !== 0) {
            deltas.push(parsed.delta);
          }
          buffer = buffer.slice(parsed.length);
          continue;
        }

        if (isIncompleteMouseInput(buffer)) {
          pending = buffer;
          break;
        }

        buffer = buffer.slice(1);
      }

      if (pending.length >= MAX_MOUSE_SEQUENCE_LENGTH) {
        pending = '';
      }

      return deltas;
    },
    reset(): void {
      pending = '';
    },
  };
}
