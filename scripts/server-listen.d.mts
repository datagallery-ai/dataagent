export function readServerListen(env?: NodeJS.ProcessEnv, configPath?: string, envFile?: string): { host: string; port: string };
export function resolveHome(env?: NodeJS.ProcessEnv): string;
export function httpOrigin(host: string, port: string | number): string;
export function writeServerListen(env: NodeJS.ProcessEnv, listen: { host: string; port: string | number }): void;
