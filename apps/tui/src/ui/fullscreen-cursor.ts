/** Ink 7.1 omits the final newline for full-height TTY frames, while its
 * cursor suffix assumes that newline. Leave the return-to-bottom prefix alone.
 * Re-run the unpatched PTY probe before upgrading Ink; remove this if fixed.
 */
export function correctFullscreenCursor(output: string): string {
  return output.replace(/\x1b\[(\d+)A(\x1b\[\d+G\x1b\[\?25h)$/, (_, rows: string, suffix: string) => {
    const up = Math.max(0, Number(rows) - 1);
    return (up ? `\x1b[${up}A` : '') + suffix;
  });
}

/** Install only for a mounted full-height App, after the opt-in redraw writer. */
export function installFullscreenCursorCorrection(
  stdout: NodeJS.WriteStream,
  enabled: () => boolean = () => true,
): () => void {
  if (!stdout.isTTY) return () => {};
  const originalWrite = stdout.write;
  let installed = true;
  const write = function (
    this: NodeJS.WriteStream,
    chunk: string | Uint8Array,
    encodingOrCallback?: BufferEncoding | ((error?: Error | null) => void),
    callback?: (error?: Error | null) => void,
  ) {
    if (installed && enabled() && typeof chunk === 'string') chunk = correctFullscreenCursor(chunk);
    return originalWrite.call(this, chunk, encodingOrCallback as BufferEncoding, callback);
  } as typeof stdout.write;
  stdout.write = write;
  return () => {
    installed = false;
    if (stdout.write === write) stdout.write = originalWrite;
  };
}
