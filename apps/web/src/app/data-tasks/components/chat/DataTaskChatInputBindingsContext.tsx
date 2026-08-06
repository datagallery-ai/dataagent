"use client";

import { createContext, useContext, type MutableRefObject, type ReactNode } from "react";
import type { EvidenceRef } from "@datafoundry/contracts";
import type {
  ChatSession,
  FileMentionResource,
  MentionResource,
  PerRunMentionKind,
  PerRunFileSelection,
  PerRunSelection,
  RunForwardedProps,
  SessionStartedHints,
  WorkspaceConfigItem,
  WorkspaceConfigStore,
} from "../../data-task-state";
import type { LiveRunStatus } from "../../live-run-state";

export type DataTaskDraftPromptRequest = {
  id: number;
  text: string;
};

export type DataTaskChatInputBindings = {
  activeLlmId: string | null;
  llmOptions: WorkspaceConfigItem[];
  onActiveLlmChange: (llmId: string) => void;
  onOpenLlmConfig: () => void;
  mentionResources: MentionResource[];
  perRunSelection: PerRunSelection;
  onTogglePerRunMention: (kind: PerRunMentionKind, id: string) => void;
  onRemovePerRunMention: (kind: PerRunMentionKind, id: string) => void;
  onClearPerRunMentions: () => void;
  fileMentionResources: FileMentionResource[];
  perRunFiles: PerRunFileSelection;
  onTogglePerRunFileMention: (resource: FileMentionResource) => void;
  onRemovePerRunFileMention: (resource: FileMentionResource) => void;
  onClearPerRunFileMentions: () => void;
  selectedEvidenceRefs: EvidenceRef[];
  onRemoveEvidenceRef: (id: string) => void;
  onClearEvidenceRefs: () => void;
  workspaceConfig: WorkspaceConfigStore;
  activeSession: ChatSession | null;
  sessionStartedHints?: SessionStartedHints;
  onToggleSessionResource: (kind: PerRunMentionKind, id: string) => void;
  draftPromptRequest: DataTaskDraftPromptRequest | null;
  onDraftPromptConsumed: (id: number) => void;
  chatColumnWidth: number;
  agentId: string;
  activeThreadId: string | null;
  capabilitiesReady: boolean;
  onUserMessageSubmitted: (text: string) => void;
  liveRunStatus: LiveRunStatus;
  liveRunRunId: string | null;
  liveRunErrorMessage: string | null;
  onCancelRun?: () => Promise<void> | void;
  stopActiveRun?: () => Promise<void>;
  stopActiveChatRunRef?: MutableRefObject<(() => void) | undefined>;
  cancelRunBusy?: boolean;
  getRunForwardedProps: () => RunForwardedProps;
};

const DataTaskChatInputBindingsContext =
  createContext<DataTaskChatInputBindings | null>(null);

export function DataTaskChatInputBindingsProvider({
  value,
  children,
}: {
  value: DataTaskChatInputBindings;
  children: ReactNode;
}) {
  return (
    <DataTaskChatInputBindingsContext.Provider value={value}>
      {children}
    </DataTaskChatInputBindingsContext.Provider>
  );
}

export function useDataTaskChatInputBindings(): DataTaskChatInputBindings {
  const value = useContext(DataTaskChatInputBindingsContext);
  if (!value) {
    throw new Error(
      "useDataTaskChatInputBindings must be used within DataTaskChatInputBindingsProvider",
    );
  }
  return value;
}
