import { describe, expect, it } from "vitest";
import {
  builtinSkillSettings,
  isSkillSettingsValid,
  parseSkillMdContent,
  skillSettingsFromPackage,
} from "../data-task-state";

describe("skill package parsing", () => {
  it("parses SKILL.md frontmatter into package settings", () => {
    const content = `---
name: 报告草稿
description: 偏向结论整理与报告产出
version: 1.2.0
allowed-tools: inspect_schema, run_sql_readonly
---

# 报告草稿

按步骤整理结论。`;

    const result = parseSkillMdContent(content, "SKILL.md");
    expect("error" in result).toBe(false);
    if ("error" in result) return;

    expect(result.name).toBe("报告草稿");
    expect(result.description).toBe("偏向结论整理与报告产出");
    expect(result.version).toBe("1.2.0");
    expect(result.allowedTools).toBe("inspect_schema, run_sql_readonly");

    const settings = skillSettingsFromPackage(result);
    expect(settings.packageFormat).toBe("skill-md");
    expect(settings.packageContent).toContain("# 报告草稿");
    expect(isSkillSettingsValid(settings)).toBe(true);
  });

  it("rejects markdown without frontmatter", () => {
    const result = parseSkillMdContent("# hello", "skill.md");
    expect(result).toEqual({
      error: "Missing YAML frontmatter (file must start and close with ---).",
    });
  });

  it("treats builtin skills as valid without local package content", () => {
    const settings = builtinSkillSettings("data-analysis");
    expect(settings.packageFormat).toBe("skill-md");
    expect(settings.packageSource).toBe("builtin://data-analysis");
    expect(isSkillSettingsValid(settings)).toBe(
      true,
    );
  });
});
