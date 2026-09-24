import { stripVTControlCharacters } from "node:util";

/** Strip terminal controls from backend diagnostics before displaying them. */
export function terminalText(text: string): string {
  return stripVTControlCharacters(text).replace(/[\u0000-\u001f\u007f-\u009f]/g, "");
}
