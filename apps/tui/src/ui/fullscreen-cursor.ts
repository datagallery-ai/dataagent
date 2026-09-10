/** Ink 7.1 counts a trailing newline when positioning the cursor, even though
 * full-height TTY frames omit that newline. Correct only its cursor suffix;
 * its return-to-bottom prefix already uses the actual final row correctly.
 * Install only while rendering a full-height App, never for inline prompts.
 */
export function installFullscreenCursorCorrection(stdout: NodeJS.WriteStream): () => void {
  if (!stdout.isTTY) return () => {};
  const originalWrite = stdout.write;
  const write = function (
    chunk: string | Uint8Array,
    encodingOrCallback?: BufferEncoding | ((error?: Error | null) => void),
    callback?: (error?: Error | null) => void,
  ) {
    if (typeof chunk === 'string') {
      chunk = chunk.replace(/\x1b\[(\d+)A(\x1b\[\d+G\x1b\[\?25h)$/, (_, rows: string, suffix: string) => {
        const up = Math.max(0, Number(rows) - 1);
        return (up ? `\x1b[${up}A` : '') + suffix;
      });
    }
    return originalWrite.call(stdout, chunk, encodingOrCallback as BufferEncoding, callback);
  } as typeof stdout.write;
  stdout.write = write;
  return () => { if (stdout.write === write) stdout.write = originalWrite; };
}
