export type LogMetadata = Record<string, unknown>;
export type Logger = {
  path: string;
  write(level: string, message: string, metadata?: LogMetadata): void;
  debug(message: string, metadata?: LogMetadata): void;
  info(message: string, metadata?: LogMetadata): void;
  warn(message: string, metadata?: LogMetadata): void;
  error(message: string, errorOrMetadata?: unknown, metadata?: LogMetadata): void;
};
export function logPath(file: "backend" | "web" | "tui", env?: NodeJS.ProcessEnv): string;
export function registerLogSecrets(values?: Iterable<unknown>): void;
export function redactLogText(value: unknown): string;
export function createLogger(options?: { file?: "backend" | "web" | "tui"; component?: string; env?: NodeJS.ProcessEnv; directory?: string; maxBytes?: number; backups?: number }): Logger;
export function captureOutput(logger: Logger, options?: { stage?: string; level?: string; echo?: { write(text: string): unknown } }): { append(chunk: unknown): void; flush(): void };
export function installConsoleLogging(options: { file: "backend" | "web" | "tui"; component?: string }): void;
