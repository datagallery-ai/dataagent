import { describe, expect, it } from "vitest";
import {
  artifactToneForType,
  btnPrimaryClass,
  btnSecondaryClass,
  choiceOptionIconClass,
  statusTone,
  stepKindTone,
} from "../ui-tokens";
import {
  PER_RUN_MENTION_APPEARANCE,
  WORKSPACE_CONFIG_BADGE_CLASS,
} from "../data-task-state";

describe("data-task UI tokens", () => {
  it("maps data step kinds to semantic tone classes", () => {
    expect(stepKindTone("inspect")).toMatchObject({
      text: "text-step-inspect",
      bg: "bg-step-inspect/10",
      border: "border-step-inspect/30",
      bar: "bg-step-inspect",
    });
    expect(stepKindTone("knowledge").text).toBe("text-step-knowledge");
    expect(stepKindTone("other").text).toBe("text-muted");
  });

  it("maps artifact types to visual metadata", () => {
    expect(artifactToneForType("dataset")).toMatchObject({
      icon: "▦",
      label: "dataset",
      text: "text-step-query",
    });
    expect(artifactToneForType("file")).toMatchObject({
      icon: "□",
      label: "file",
    });
  });

  it("maps status kinds to semantic surface classes", () => {
    expect(statusTone("success")).toContain("step-success");
    expect(statusTone("error")).toContain("step-error");
    expect(statusTone("muted")).toContain("border-border");
  });

  it("uses neutral chrome for workspace resource badges and mention pills", () => {
    const colorfulUtilityPattern = /\b(?:sky|violet|emerald|red|amber|indigo)-/u;

    expect(Object.values(WORKSPACE_CONFIG_BADGE_CLASS).join(" ")).not.toMatch(
      colorfulUtilityPattern,
    );
    expect(
      Object.values(PER_RUN_MENTION_APPEARANCE)
        .flatMap((appearance) => [
          appearance.badge,
          appearance.pill,
          appearance.pillOpen,
          appearance.chip,
        ])
        .join(" "),
    ).not.toMatch(colorfulUtilityPattern);
  });

  it("keeps shared action chrome neutral for Codex-style panels", () => {
    expect(btnPrimaryClass).toContain("bg-primary");
    expect(btnPrimaryClass).not.toMatch(/\bblue-|sky-|violet-|amber-/u);
    expect(btnSecondaryClass).toContain("border-border");
    expect(choiceOptionIconClass).toContain("bg-surface");
    expect(choiceOptionIconClass).not.toMatch(/\bprimary\/30|primary-light\/12/u);
  });
});
