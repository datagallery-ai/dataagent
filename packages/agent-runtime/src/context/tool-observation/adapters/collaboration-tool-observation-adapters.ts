import { asRecord, BaseToolObservationAdapter, pickFields } from "./base-tool-observation-adapter.js";

abstract class BaseCollaborationToolObservationAdapter extends BaseToolObservationAdapter {
  protected project(raw: unknown): unknown {
    return {
      ...pickFields(asRecord(raw), ["content", "isError"]),
      source: "mastra-collaboration"
    };
  }
}

export class AskUserToolObservationAdapter extends BaseCollaborationToolObservationAdapter {
  readonly toolName = "ask_user";
  readonly resultType = "ask-user";
}

export class SubmitPlanToolObservationAdapter extends BaseCollaborationToolObservationAdapter {
  readonly toolName = "submit_plan";
  readonly resultType = "submit-plan";
}
