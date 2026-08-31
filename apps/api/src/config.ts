import { createEnvConfig } from "@datafoundry/contracts";

export type ApiConfig = {
  host: string;
  port: number;
};

export const loadApiConfig = (): ApiConfig => createEnvConfig(process.env).api;
