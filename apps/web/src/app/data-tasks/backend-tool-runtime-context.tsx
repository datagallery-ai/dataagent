"use client";

import { createContext, useContext } from "react";
import type { BackendToolPhase } from "./tool-call-display";

export type BackendToolRuntime = {
  phases: ReadonlyMap<string, BackendToolPhase>;
  results: ReadonlyMap<string, string>;
};

const defaultRuntime: BackendToolRuntime = {
  phases: new Map(),
  results: new Map(),
};

const BackendToolRuntimeContext =
  createContext<BackendToolRuntime>(defaultRuntime);

export function BackendToolRuntimeProvider({
  runtime,
  children,
}: {
  runtime: BackendToolRuntime;
  children: React.ReactNode;
}) {
  return (
    <BackendToolRuntimeContext.Provider value={runtime}>
      {children}
    </BackendToolRuntimeContext.Provider>
  );
}

function useBackendToolRuntime(): BackendToolRuntime {
  return useContext(BackendToolRuntimeContext);
}

export function useBackendToolPhase(
  toolCallId?: string,
): BackendToolPhase | undefined {
  const { phases } = useBackendToolRuntime();
  if (!toolCallId) return undefined;
  return phases.get(toolCallId);
}

export function useBackendToolResult(toolCallId?: string): string | undefined {
  const { results } = useBackendToolRuntime();
  if (!toolCallId) return undefined;
  return results.get(toolCallId);
}
