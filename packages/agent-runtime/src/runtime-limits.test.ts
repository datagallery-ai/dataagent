import { describe, expect, it } from "vitest";

import { AGENT_MAX_STEPS, SQL_MAX_EXECUTION_COUNT } from "./runtime-limits.js";
import {
  AGENT_RUNTIME_LIMIT_DEFINITIONS,
  AGENT_RUNTIME_LIMITS
} from "./config/agent-runtime-limits.js";

describe("data analysis runtime limits", () => {
  it("reserves enough headroom for complex analyses to execute and commit their final claims", () => {
    expect(AGENT_MAX_STEPS).toBeGreaterThanOrEqual(80);
    expect(SQL_MAX_EXECUTION_COUNT).toBeGreaterThanOrEqual(60);
  });

  it("keeps every runtime and context limit in one documented configuration registry", () => {
    expect(Object.keys(AGENT_RUNTIME_LIMIT_DEFINITIONS)).toEqual(Object.keys(AGENT_RUNTIME_LIMITS));
    for (const definition of Object.values(AGENT_RUNTIME_LIMIT_DEFINITIONS)) {
      expect(definition.env).toMatch(/^DATAFOUNDRY_[A-Z0-9_]+$/u);
      expect(definition.description.length).toBeGreaterThan(20);
      expect(definition.defaultValue).toBeGreaterThanOrEqual(definition.min);
      expect(definition.defaultValue).toBeLessThanOrEqual(definition.max);
    }
  });

  it("centralizes the contract grounding attempt budget", () => {
    expect(AGENT_RUNTIME_LIMIT_DEFINITIONS.contractGrounderMaxAttempts).toMatchObject({
      defaultValue: 2,
      env: "DATAFOUNDRY_CONTRACT_GROUNDER_MAX_ATTEMPTS"
    });
    expect(AGENT_RUNTIME_LIMITS.contractGrounderMaxAttempts).toBe(2);
  });
});
