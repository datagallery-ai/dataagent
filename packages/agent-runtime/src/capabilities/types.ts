import type { z } from "zod";

export type CapabilityExposure = "agent" | "runtime" | "both";

export type CapabilityPluginManifest = {
  id: string;
  version: string;
  provides: string[];
  requires?: Array<{ id: string; version?: string }>;
};

export type CapabilityActionGuardResult =
  | { allowed: true }
  | { allowed: false; reasonCode: string; message?: string };

export type CapabilityActionDefinition = {
  name: string;
  exposure: CapabilityExposure;
  inputSchema: z.ZodType;
  outputSchema: z.ZodType;
  idempotency: "required" | "supported" | "none";
  guard?(input: unknown): CapabilityActionGuardResult | Promise<CapabilityActionGuardResult>;
  execute(context: CapabilityExecutionContext, input: unknown): Promise<unknown>;
  reduce?(domainState: unknown, result: unknown): unknown;
  projectObservation?(result: unknown): unknown;
};

export type CapabilityExecutionContext = {
  actionId: string;
  actionName: string;
  runId: string;
  segmentId: string;
  abortSignal?: AbortSignal;
  invocationArgs?: unknown[];
};

export type CapabilityPlugin = {
  manifest: CapabilityPluginManifest;
  actions: CapabilityActionDefinition[];
  initialize?(): Promise<void>;
  dispose?(): Promise<void>;
};

export type RegisteredCapabilityAction = {
  action: CapabilityActionDefinition;
  pluginId: string;
  pluginVersion: string;
};
