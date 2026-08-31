import { describe, expect, it } from "vitest";

import {
  ActionRouter,
  CapabilityRegistry,
  DataLinkSemanticProvider,
  InMemoryProtocolStateStore,
  ProtocolRegistry,
  ProtocolHandoffCoordinator,
  ProtocolRouter,
  ProtocolRuntime,
  LocalSemanticProvider,
  SemanticProviderChain,
  evaluateProtocolHandoff,
  createToolCapabilityPlugin,
  validateProtocolDefinition
} from "../testing.js";

describe("protocol testing exports", () => {
  it("exposes the protocol kernel through the package testing surface", () => {
    expect(ProtocolRegistry).toBeTypeOf("function");
    expect(ProtocolRouter).toBeTypeOf("function");
    expect(ProtocolRuntime).toBeTypeOf("function");
    expect(ProtocolHandoffCoordinator).toBeTypeOf("function");
    expect(InMemoryProtocolStateStore).toBeTypeOf("function");
    expect(validateProtocolDefinition).toBeTypeOf("function");
    expect(evaluateProtocolHandoff).toBeTypeOf("function");
    expect(CapabilityRegistry).toBeTypeOf("function");
    expect(ActionRouter).toBeTypeOf("function");
    expect(createToolCapabilityPlugin).toBeTypeOf("function");
    expect(DataLinkSemanticProvider).toBeTypeOf("function");
    expect(LocalSemanticProvider).toBeTypeOf("function");
    expect(SemanticProviderChain).toBeTypeOf("function");
  });
});
