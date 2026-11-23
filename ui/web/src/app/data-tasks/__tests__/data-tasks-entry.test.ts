import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

describe("data-tasks production entry", () => {
  it("keeps the route module as a thin dynamic import shell", () => {
    const source = readFileSync(
      join(__dirname, "../page.tsx"),
      "utf8",
    );
    expect(source).toContain('nextDynamic(() => import("./data-tasks-app")');
    expect(source).not.toContain("@copilotkit/react-core");
    expect(source.split("\n").length).toBeLessThan(40);
  });
});
