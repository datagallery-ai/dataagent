import { describe, expect, it } from "vitest";

import { createTranslator } from "../../../i18n/translate";
import { dataStepKindForTool, dataStepLabel, toolDisplayTitle } from "../data-task-state";

describe("Deep Agents tool alignment", () => {
  it("titles Deep Agents builtin tools with the existing workspace vocabulary", () => {
    const t = createTranslator("en");
    expect(toolDisplayTitle("ls", t)).toBe("Browse files");
    expect(toolDisplayTitle("glob", t)).toBe("Match files");
    expect(toolDisplayTitle("execute", t)).toBe("Run command");
    expect(toolDisplayTitle("task", t)).toBe("Delegate task");
  });

  it("classifies filesystem tools as workspace steps instead of data operations", () => {
    expect(dataStepKindForTool("ls")).toBe("workspace");
    expect(dataStepKindForTool("glob")).toBe("workspace");
    expect(dataStepKindForTool("execute")).toBe("workspace");
    expect(dataStepKindForTool("write_file")).toBe("workspace");
    expect(dataStepKindForTool("read_file")).toBe("workspace");
    expect(dataStepKindForTool("inspect_schema")).toBe("inspect");
    expect(dataStepLabel("workspace")).toBe("Workspace");
  });
});
