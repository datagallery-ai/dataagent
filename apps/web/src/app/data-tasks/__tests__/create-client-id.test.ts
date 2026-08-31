import { afterEach, describe, expect, it, vi } from "vitest";
import { createClientId } from "../data-task-state";

describe("createClientId", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("falls back when crypto.randomUUID is missing (insecure HTTP)", () => {
    vi.stubGlobal("crypto", {});

    const id = createClientId("msg");

    expect(id).toMatch(/^msg-\d+-[0-9a-f]+$/);
  });

  it("falls back when crypto.randomUUID throws in a non-secure context", () => {
    vi.stubGlobal("crypto", {
      randomUUID: () => {
        throw new Error("Secure context required");
      },
    });

    const id = createClientId();

    expect(id).toMatch(/^id-\d+-[0-9a-f]+$/);
  });

  it("uses crypto.randomUUID when available", () => {
    vi.stubGlobal("crypto", {
      randomUUID: () => "11111111-2222-3333-4444-555555555555",
    });

    expect(createClientId()).toBe("11111111-2222-3333-4444-555555555555");
  });
});
