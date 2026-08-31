import { describe, expect, it } from "vitest";

import { normalizeTraceDagDto } from "../client";

describe("normalizeTraceDagDto", () => {
  it("keeps old trace responses renderable when sections are absent", () => {
    const dag = normalizeTraceDagDto({
      sessionId: "session-1",
      nodes: [],
      edges: [],
    });

    expect(dag.sections).toEqual([]);
  });
});
