export function readServerListen(env?: NodeJS.ProcessEnv, configPath?: string, envFile?: string): { host: string; port: string };
export function resolveHome(env?: NodeJS.ProcessEnv): string;
export function httpOrigin(host: string, port: string | number): string;
