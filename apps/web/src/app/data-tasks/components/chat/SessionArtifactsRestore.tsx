"use client";

import { useEffect, useRef } from "react";
import { configApi } from "../../../../lib/config-api";
import { hasCapability } from "../../data-task-state";
import { reconcileLiveRunArtifacts, reduceLiveRunEvent } from "../../live-run-state";
import { useLiveRun, useLiveRunSetters } from "../../use-data-foundry-run";

export function SessionArtifactsRestore({
  capabilitiesReady,
  threadId,
}: {
  capabilitiesReady: boolean;
  threadId?: string | null;
}) {
  const { liveRun } = useLiveRun(threadId);
  const { setLiveRunForThread } = useLiveRunSetters();
  const requestIdRef = useRef(0);
  const toolCallCountRef = useRef(0);

  useEffect(() => {
    toolCallCountRef.current = 0;
  }, [threadId]);

  useEffect(() => {
    if (!threadId || !capabilitiesReady || !hasCapability("artifact.list")) {
      return;
    }

    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    let cancelled = false;

    void configApi.listSessionArtifacts(threadId)
      .then((response) => {
        if (cancelled || requestIdRef.current !== requestId) return;
        setLiveRunForThread(threadId, (current) => {
          const base = { ...current, artifacts: [] as typeof current.artifacts };
          return reconcileLiveRunArtifacts(
            (response.artifacts ?? []).reduce(
              (state, artifact) =>
                reduceLiveRunEvent(state, {
                  type: "CUSTOM",
                  name: "artifact",
                  value: {
                    id: artifact.id,
                    type: artifact.type,
                    name: artifact.name,
                    title: artifact.name,
                    file_id: artifact.fileId,
                    download_url: artifact.downloadUrl,
                    preview_json: artifact.preview_json,
                    preview_available:
                      artifact.preview_json !== undefined && artifact.preview_json !== null
                        ? true
                        : Boolean(artifact.fileId),
                    // R-018: authoritative origin so the artifact links to its tool call
                    // without heuristics.
                    ...(artifact.toolCallId ? { tool_call_id: artifact.toolCallId } : {}),
                    ...(artifact.stepId ? { step_id: artifact.stepId } : {}),
                    ...(artifact.runId ? { run_id: artifact.runId } : {}),
                    ...(artifact.logicalKey ? { logical_key: artifact.logicalKey } : {}),
                    ...(artifact.versionCount !== undefined ? { version: artifact.versionCount } : {}),
                    ...(artifact.createdAt ? { created_at: artifact.createdAt } : {}),
                  },
                }),
              base,
            ),
          );
        });
      })
      .catch(() => undefined);

    return () => {
      cancelled = true;
    };
  }, [capabilitiesReady, setLiveRunForThread, threadId]);

  // Re-link artifacts when tool calls arrive after artifact list restore.
  useEffect(() => {
    if (!threadId || !capabilitiesReady) {
      return;
    }
    const toolCallCount = liveRun.toolCalls.length;
    if (toolCallCount === 0 || toolCallCount === toolCallCountRef.current) {
      return;
    }
    toolCallCountRef.current = toolCallCount;
    setLiveRunForThread(threadId, (current) => reconcileLiveRunArtifacts(current));
  }, [capabilitiesReady, liveRun.toolCalls.length, setLiveRunForThread, threadId]);

  return null;
}
