import { DATA_ACTIONS } from "../data-actions.js";
import type { AgentProtocolDefinition } from "../types.js";

export type GeneralTaskState = {
  answerMessageId?: string;
};

export const createGeneralTaskProtocol = (
  availableActionNames: string[]
): AgentProtocolDefinition<GeneralTaskState> => {
  const workActions = [
    ...availableActionNames.filter((actionName) => !DATA_ACTIONS.has(actionName)),
    "protocol.handoff.propose",
    "general.answer.commit"
  ];
  return {
    id: "general-task",
    version: "1",
    initialPhase: "understand",
    phases: {
      understand: {
        allowedActions: workActions,
        transitions: [
          { targetPhase: "answer", when: ({ actionName }) => actionName === "general.answer.commit" },
          { targetPhase: "gather", when: ({ actionName }) => actionName !== "general.answer.commit" }
        ]
      },
      gather: {
        allowedActions: workActions,
        transitions: [{
          targetPhase: "answer",
          when: ({ actionName }) => actionName === "general.answer.commit"
        }]
      },
      answer: { allowedActions: [], transitions: [] }
    },
    createInitialState: () => ({}),
    completionPolicy: ({ contextPackageRef, state }) => state.answerMessageId
      ? { status: "completed", evaluatedContextPackageRef: contextPackageRef, evidenceRefs: [] }
      : {
          status: "continue",
          reasons: ["GENERAL_ANSWER_NOT_COMMITTED"],
          allowedActions: ["general.answer.commit"]
        }
  };
};

export const reduceGeneralTaskAction = (
  state: GeneralTaskState,
  actionName: string,
  result: unknown
): GeneralTaskState => {
  if (actionName !== "general.answer.commit") {
    return state;
  }
  const messageId = recordString(result, "messageId");
  if (!messageId) {
    throw new Error("GENERAL_ANSWER_MESSAGE_MISSING");
  }
  return { ...state, answerMessageId: messageId };
};

const recordString = (value: unknown, key: string): string | undefined => {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return undefined;
  }
  const field = (value as Record<string, unknown>)[key];
  return typeof field === "string" && field.length > 0 ? field : undefined;
};
