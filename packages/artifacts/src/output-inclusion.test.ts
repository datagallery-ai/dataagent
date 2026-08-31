import { describe, expect, it } from "vitest";

import {
  inferOutputTypeFromPath,
  shouldIngestSessionOutputPath
} from "./output-inclusion.js";

describe("shouldIngestSessionOutputPath", () => {
  it("allows deliverable extensions", () => {
    expect(shouldIngestSessionOutputPath("reports/summary.md")).toBe(true);
    expect(shouldIngestSessionOutputPath("out/data.csv")).toBe(true);
    expect(shouldIngestSessionOutputPath("chart.png")).toBe(true);
  });

  it("excludes scripts and scratch paths", () => {
    expect(shouldIngestSessionOutputPath("analysis.py")).toBe(false);
    expect(shouldIngestSessionOutputPath("scratch/notes.md")).toBe(false);
    expect(shouldIngestSessionOutputPath("tmp/a.csv")).toBe(false);
  });

  it("excludes unknown or extensionless paths by default", () => {
    expect(shouldIngestSessionOutputPath("README")).toBe(false);
    expect(shouldIngestSessionOutputPath("data.bin")).toBe(false);
  });
});

describe("inferOutputTypeFromPath", () => {
  it("maps extensions to artifact types", () => {
    expect(inferOutputTypeFromPath("a.md")).toBe("markdown");
    expect(inferOutputTypeFromPath("a.html")).toBe("html");
    expect(inferOutputTypeFromPath("a.png")).toBe("image");
    expect(inferOutputTypeFromPath("a.csv")).toBe("file");
  });
});
