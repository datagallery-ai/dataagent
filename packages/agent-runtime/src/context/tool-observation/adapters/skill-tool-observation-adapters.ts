import { asRecord, BaseToolObservationAdapter, pickFields } from "./base-tool-observation-adapter.js";

abstract class BaseSkillToolObservationAdapter extends BaseToolObservationAdapter {
  protected readonly sourceOwner = "skill";

  protected project(raw: unknown): unknown {
    return {
      ...pickFields(asRecord(raw), [
        "name",
        "query",
        "results",
        "skillName",
        "path",
        "startLine",
        "endLine",
        "content",
        "value",
        "preview",
        "truncated"
      ]),
      source: "mastra-skill"
    };
  }

  protected createDedupeKeys(projected: unknown): string[] {
    const record = asRecord(projected);
    return [`skill:${this.toolName}:${String(record.name ?? record.skillName ?? record.path ?? this.toolName)}`];
  }

  protected createExclusivityKey(projected: unknown): string {
    const record = asRecord(projected);
    return `skill:${this.toolName}:${String(record.name ?? record.skillName ?? record.path ?? this.toolName)}`;
  }
}

export class SkillActivationToolObservationAdapter extends BaseSkillToolObservationAdapter {
  readonly toolName = "skill";
  readonly resultType = "skill-activation";
}

export class SkillSearchToolObservationAdapter extends BaseSkillToolObservationAdapter {
  readonly toolName = "skill_search";
  readonly resultType = "skill-search";
}

export class SkillReadToolObservationAdapter extends BaseSkillToolObservationAdapter {
  readonly toolName = "skill_read";
  readonly resultType = "skill-read";
}
