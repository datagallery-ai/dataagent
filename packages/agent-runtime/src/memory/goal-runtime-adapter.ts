import type { Agent } from "@mastra/core/agent";

export type GoalRequest = {
  maxRuns?: number;
  objective: string;
};

export type GoalSnapshot = {
  max_runs?: number;
  objective: string;
  runs_used: number;
  started_at: number;
  status: "active" | "paused" | "done";
  updated_at: number;
};

export class GoalRuntimeAdapter {
  constructor(
    private readonly agent: Agent,
    private readonly resourceId: string,
    private readonly threadId: string
  ) {}

  /** Set a trusted thread objective through Mastra's experimental goal API. */
  async setObjective(request: GoalRequest): Promise<GoalSnapshot | undefined> {
    const record = await this.agent.setObjective(request.objective, {
      threadId: this.threadId,
      resourceId: this.resourceId,
      ...(request.maxRuns ? { maxRuns: request.maxRuns } : {})
    });
    return projectGoalRecord(record);
  }

  /** Read the current objective without exposing Mastra's experimental record type. */
  async getSnapshot(): Promise<GoalSnapshot | undefined> {
    return projectGoalRecord(await this.agent.getObjective({ threadId: this.threadId }));
  }
}

type GoalRecord = Awaited<ReturnType<Agent["getObjective"]>>;

const projectGoalRecord = (record: GoalRecord): GoalSnapshot | undefined => {
  if (!record) {
    return undefined;
  }
  return {
    ...(record.maxRuns !== undefined ? { max_runs: record.maxRuns } : {}),
    objective: record.objective,
    runs_used: record.runsUsed,
    started_at: record.startedAt,
    status: record.status,
    updated_at: record.updatedAt
  };
};
