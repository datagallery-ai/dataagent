import { asRecord, BaseToolObservationAdapter, pickFields } from "./base-tool-observation-adapter.js";

abstract class BaseTaskToolObservationAdapter extends BaseToolObservationAdapter {
  protected project(raw: unknown): unknown {
    const record = asRecord(raw);
    return {
      ...pickFields(record, ["content", "tasks", "summary", "incompleteTasks", "isError"]),
      source: "mastra-task-state"
    };
  }
}

export class TaskWriteToolObservationAdapter extends BaseTaskToolObservationAdapter {
  readonly toolName = "task_write";
  readonly resultType = "task-write";
}

export class TaskUpdateToolObservationAdapter extends BaseTaskToolObservationAdapter {
  readonly toolName = "task_update";
  readonly resultType = "task-update";
}

export class TaskCompleteToolObservationAdapter extends BaseTaskToolObservationAdapter {
  readonly toolName = "task_complete";
  readonly resultType = "task-complete";
}

export class TaskCheckToolObservationAdapter extends BaseTaskToolObservationAdapter {
  readonly toolName = "task_check";
  readonly resultType = "task-check";
}
