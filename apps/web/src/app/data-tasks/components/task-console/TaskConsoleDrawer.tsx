import { useEffect } from "react";
import type { EvidenceRef } from "@datafoundry/contracts";
import type { DataArtifact, TimelineEvent } from "../../data-task-state";
import type { JobDto } from "../../../../lib/config-api";
import type { LiveRun, SessionUsageStats } from "../../live-run-state";
import type { ProcessToolGroup } from "../../process-tool-groups";
import type { TaskSelection } from "../../data-tasks-app";
import { overlayBackdropClass, overlayPanelClass } from "../../ui-tokens";
import { TaskConsolePanel } from "./TaskConsolePanel";

type TaskConsoleDrawerProps = {
  sessionId: string;
  runId?: string;
  onReferenceEvidence: (ref: EvidenceRef) => void;
  artifacts: DataArtifact[];
  liveRun: LiveRun;
  toolGroups: ProcessToolGroup[];
  sessionUsage: SessionUsageStats;
  selection: TaskSelection;
  visibleEvents: TimelineEvent[];
  currentQuestion?: string;
  artifactFocusId?: string | null;
  onArtifactFocusHandled?: () => void;
  onClearSelection: () => void;
  onMentionArtifact?: (artifact: DataArtifact) => void;
  isOpen: boolean;
  onClose: () => void;
  onOpenTrace: () => void;
  onCreateCheckpointBranch?: (checkpointId: string) => Promise<void> | void;
  onPromoteArtifact?: (artifact: DataArtifact) => Promise<void> | void;
  onArtifactExportJob?: (job: JobDto) => void;
  onSelectEvent: (eventId: string) => void;
  onSelectToolGroup: (groupId: string) => void;
  promotedArtifactIds?: ReadonlySet<string>;
};

export function TaskConsoleDrawer({
  sessionId,
  runId,
  onReferenceEvidence,
  artifacts,
  liveRun,
  toolGroups,
  sessionUsage,
  selection,
  visibleEvents,
  currentQuestion,
  artifactFocusId,
  onArtifactFocusHandled,
  onClearSelection,
  onMentionArtifact,
  isOpen,
  onClose,
  onOpenTrace,
  onCreateCheckpointBranch,
  onPromoteArtifact,
  onArtifactExportJob,
  onSelectEvent,
  onSelectToolGroup,
  promotedArtifactIds,
}: TaskConsoleDrawerProps) {
  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      // Nested overlays (preview / trace) handle Escape first via capture +
      // stopImmediatePropagation; skip if already handled.
      if (event.key !== "Escape" || event.defaultPrevented) return;
      onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div
      className={`${overlayBackdropClass} p-2 sm:p-4`}
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className={`mx-auto h-full max-w-lg sm:max-w-xl ${overlayPanelClass}`}>
        <TaskConsolePanel
          sessionId={sessionId}
          runId={runId}
          onReferenceEvidence={onReferenceEvidence}
          artifacts={artifacts}
          liveRun={liveRun}
          toolGroups={toolGroups}
          sessionUsage={sessionUsage}
          selection={selection}
          visibleEvents={visibleEvents}
          currentQuestion={currentQuestion}
          artifactFocusId={artifactFocusId}
          onArtifactFocusHandled={onArtifactFocusHandled}
          onClearSelection={onClearSelection}
          onClose={onClose}
          onMentionArtifact={onMentionArtifact}
          onOpenTrace={onOpenTrace}
          onCreateCheckpointBranch={onCreateCheckpointBranch}
          onPromoteArtifact={onPromoteArtifact}
          onArtifactExportJob={onArtifactExportJob}
          onSelectEvent={onSelectEvent}
          onSelectToolGroup={onSelectToolGroup}
          promotedArtifactIds={promotedArtifactIds}
        />
      </div>
    </div>
  );
}
