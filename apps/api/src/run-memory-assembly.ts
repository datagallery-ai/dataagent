import { type BaseEvent, type RunAgentInput } from "@ag-ui/client";
import type { EvidenceRef } from "@datafoundry/contracts";
import {
  createCustomEvent,
  createMastraConversationMemoryBridge,
  type AgentMemoryMode,
  type TaskStateRuntime
} from "@datafoundry/agent-runtime";
import {
  type ConversationMessageRecord,
  type LongTermMemoryRecord,
  type MetadataStore
} from "@datafoundry/metadata";

import {
  ConversationMemoryService,
  type ConversationMemoryEventObserver
} from "./conversation-memory.js";
import { createMastraConversationSummarizer } from "./conversation-summarizer.js";
import {
  latestVisibleConversationSummary,
  listVisibleConversationMessages,
  resolveSessionLineage
} from "./session-branching.js";
import { LongTermMemoryService } from "./long-term-memory.js";
import { createMastraLongTermMemoryExtractor } from "./long-term-memory-extractor.js";

export type RunMemoryAssembly = {
  conversationMemoryObserver: ConversationMemoryEventObserver;
  conversationMessages: RunAgentInput["messages"];
  longTermMemories: LongTermMemoryRecord[];
  flushCompletedMemory(input: { emit(event: BaseEvent): void; signal: AbortSignal }): Promise<void>;
  flushDraftsMemory(): ConversationMessageRecord[];
};

type CreateRunMemoryAssemblyInput = {
  conversationMemoryMode: AgentMemoryMode;
  isResume: boolean;
  metadataStore: MetadataStore;
  model: unknown;
  modelName: string;
  modelTemperature?: number | undefined;
  runId: string;
  runInput: RunAgentInput;
  selectedDatasourceId?: string;
  sessionId: string;
  taskStateRuntime: TaskStateRuntime;
  userId: string;
  userInput: string;
  evidenceRefs?: EvidenceRef[];
};

/** Assemble prompt memory, long-term recall, event observation, and completed-run memory flushing. */
export const createRunMemoryAssembly = async (
  input: CreateRunMemoryAssemblyInput
): Promise<RunMemoryAssembly> => {
  const conversationMemory = new ConversationMemoryService({
    compactMemorySource: input.conversationMemoryMode === "working-memory-readonly"
      ? "mastra-working-memory"
      : "metadata-summary",
    memoryBridge: input.conversationMemoryMode === "off"
      ? undefined
      : createMastraConversationMemoryBridge({
        memory: input.taskStateRuntime.memory
      }),
    historyProvider: ({ excludeRunId, limit, sessionId, userId }) => {
      const lineage = resolveSessionLineage({
        metadataStore: input.metadataStore,
        sessionId,
        userId
      });
      const summary = latestVisibleConversationSummary({
        lineage,
        metadataStore: input.metadataStore,
        sessionId,
        userId
      });
      return {
        history: listVisibleConversationMessages({
          excludeRunId,
          lineage,
          limit,
          metadataStore: input.metadataStore,
          userId
        }),
        ...(summary ? { summary } : {})
      };
    },
    repository: input.metadataStore.conversationMessages,
    sessionId: input.sessionId,
    summarizer: createMastraConversationSummarizer({
      model: input.model,
      temperature: input.modelTemperature ?? 0.1
    }),
    summaryRepository: input.metadataStore.conversationSummaries,
    userId: input.userId
  });
  const longTermMemoryWriter = new LongTermMemoryService({
    extractor: createMastraLongTermMemoryExtractor({
      model: input.model,
      temperature: 0
    }),
    repository: input.metadataStore.longTermMemories
  });
  let currentUserRecord: ConversationMessageRecord | undefined;

  if (!input.isResume) {
    currentUserRecord = conversationMemory.persistCurrentUserMessage({
      currentUserText: input.userInput,
      evidenceRefs: input.evidenceRefs ?? [],
      runId: input.runId,
      runInput: input.runInput
    });
    input.metadataStore.sessions.touchLastMessage({
      user_id: input.userId,
      session_id: input.sessionId,
      last_message_at: currentUserRecord.created_at
    });
    if (input.conversationMemoryMode === "working-memory-readonly") {
      await conversationMemory.syncLatestSummaryToMemory().catch(() => false);
    }
  }

  const conversationMessages = input.isResume
    ? input.runInput.messages
    : conversationMemory.buildRunMessages({
      currentUserText: input.userInput,
      modelName: input.modelName,
      runId: input.runId,
      runInput: input.runInput
    }).messages;
  if (!input.isResume) {
    assertCompactMemoryPromptBoundary(conversationMessages, input.conversationMemoryMode);
  }

  const conversationMemoryObserver = conversationMemory.createEventObserver({ runId: input.runId });
  const longTermMemories = resolveLongTermMemories({
    ...(input.selectedDatasourceId ? { datasourceId: input.selectedDatasourceId } : {}),
    metadataStore: input.metadataStore,
    sessionId: input.sessionId,
    userId: input.userId,
    userInput: input.userInput
  });

  return {
    conversationMemoryObserver,
    conversationMessages,
    longTermMemories,
    flushDraftsMemory: () => {
      const assistantRecords = conversationMemoryObserver.flushDrafts();
      const lastAssistantRecord = assistantRecords.at(-1);
      if (lastAssistantRecord) {
        input.metadataStore.sessions.touchLastMessage({
          user_id: input.userId,
          session_id: input.sessionId,
          last_message_at: lastAssistantRecord.created_at
        });
      }
      return assistantRecords;
    },
    flushCompletedMemory: async ({ emit, signal }) => {
      const assistantRecords = await conversationMemoryObserver.flushCompleted({ signal });
      const lastAssistantRecord = assistantRecords.at(-1);
      if (lastAssistantRecord) {
        input.metadataStore.sessions.touchLastMessage({
          user_id: input.userId,
          session_id: input.sessionId,
          last_message_at: lastAssistantRecord.created_at
        });
      }
      throwIfAborted(signal);
      const extractedMemories = await longTermMemoryWriter.extractAndPersist({
        assistantRecords,
        currentUserRecord,
        ...(input.selectedDatasourceId ? { datasourceId: input.selectedDatasourceId } : {}),
        runId: input.runId,
        sessionId: input.sessionId,
        signal,
        userId: input.userId
      }).catch(() => []);
      throwIfAborted(signal);
      if (extractedMemories.length > 0) {
        emit(createCustomEvent("memory.long-term.extracted", {
          count: extractedMemories.length,
          memory_ids: extractedMemories.map((memory) => memory.id),
          source: "completed-run"
        }));
      }
    }
  };
};

const resolveLongTermMemories = (input: {
  datasourceId?: string;
  metadataStore: MetadataStore;
  sessionId: string;
  userId: string;
  userInput: string;
}): LongTermMemoryRecord[] => {
  const memories = input.metadataStore.longTermMemories.listRelevant({
    user_id: input.userId,
    session_id: input.sessionId,
    ...(input.datasourceId ? { datasource_id: input.datasourceId } : {}),
    query: input.userInput,
    limit: 6
  });
  input.metadataStore.longTermMemories.markAccessed({
    user_id: input.userId,
    memory_ids: memories.map((memory) => memory.id)
  });
  return memories;
};

const assertCompactMemoryPromptBoundary = (
  messages: RunAgentInput["messages"],
  conversationMemoryMode: AgentMemoryMode
): void => {
  const metadataSummaryCount = messages.filter((message) =>
    typeof message.id === "string" && message.id.startsWith("memory-summary:")
  ).length;
  if (metadataSummaryCount > 1) {
    throw new Error("CONVERSATION_MEMORY_DUPLICATE_METADATA_SUMMARY");
  }
  if (conversationMemoryMode === "working-memory-readonly" && metadataSummaryCount > 0) {
    throw new Error("CONVERSATION_MEMORY_DUPLICATE_COMPACT_SOURCE");
  }
};

const throwIfAborted = (signal: AbortSignal): void => {
  if (signal.aborted) {
    throw signal.reason instanceof Error ? signal.reason : new Error("MEMORY_EXTRACTION_ABORTED");
  }
};
