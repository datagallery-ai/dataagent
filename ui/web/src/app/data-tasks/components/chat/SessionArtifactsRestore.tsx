"use client";

import { useEffect, useRef } from "react";
import { configApi } from "../../../../lib/config-api";
import { hasCapability } from "../../data-task-state";
import { reconcileLiveRunArtifacts, reduceLiveRunEvent } from "../../live-run-state";
import {
  isSessionOutputKey,
  sessionOutputArtifactValue,
  sessionOutputPreviewPaths,
} from "../../session-outputs";
import { useLiveRun, useLiveRunSetters } from "../../use-data-gallery-run";

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

  // Without the /api/v1 artifact API, list the files the agent wrote to the session output
  // directory instead. Re-list whenever a run settles so new files appear without a reload.
  const runSettled = liveRun.runStatus !== "running" && liveRun.runStatus !== "suspended";
  useEffect(() => {
    if (!threadId || !capabilitiesReady || hasCapability("artifact.list") || !runSettled) {
      return;
    }

    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    let cancelled = false;

    void (async () => {
      const files = await configApi.listSessionOutputs(threadId);
      const previewPaths = new Set(sessionOutputPreviewPaths(files));
      const previews = new Map(
        await Promise.all(
          files
            .filter((file) => previewPaths.has(file.path))
            .map(async (file) => {
              try {
                return [file.path, await configApi.previewSessionOutput(threadId, file.path)] as const;
              } catch {
                return [file.path, undefined] as const;
              }
            }),
        ),
      );
      if (cancelled || requestIdRef.current !== requestId) return;
      setLiveRunForThread(threadId, (current) => {
        const base = {
          ...current,
          artifacts: current.artifacts.filter((artifact) => !isSessionOutputKey(artifact.logicalKey)),
        };
        // Oldest first so the newest file wins any ordering ties in the Outputs list.
        return reconcileLiveRunArtifacts(
          [...files].reverse().reduce(
            (state, file) =>
              reduceLiveRunEvent(state, {
                type: "CUSTOM",
                name: "artifact",
                value: sessionOutputArtifactValue(file, previews.get(file.path)),
              }),
            base,
          ),
        );
      });
    })().catch(() => undefined);

    return () => {
      cancelled = true;
    };
  }, [capabilitiesReady, runSettled, liveRun.runId, setLiveRunForThread, threadId]);

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
