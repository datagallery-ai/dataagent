import { EventType } from "@ag-ui/core";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import {
  maybeIngestSessionFileOutput,
  maybeIngestSessionFileToolResult
} from "./session-output-ingest.js";
import type { AgentRunContext } from "../types.js";

const runContext: AgentRunContext = {
  user_id: "user-1",
  workspace_id: "workspace-1",
  session_id: "session-1",
  run_id: "run-1",
  user_input: "test",
  chat_mode: "copilotkit"
};

describe("maybeIngestSessionFileOutput", () => {
  it("ingests successful write_file metadata and emits an artifact event", async () => {
    const emitted: unknown[] = [];
    const calls: unknown[] = [];

    await maybeIngestSessionFileOutput({
      metadata: {
        toolName: "write_file",
        toolCallId: "tool-1",
        path: "reports/summary.md"
      },
      sessionDir: "/tmp/session",
      runContext,
      emitter: { emit: (event) => emitted.push(event) },
      sessionOutputService: {
        upsertFromSessionFile: async (input) => {
          calls.push(input);
          return {
            artifact: {
              id: "artifact-1",
              type: "markdown",
              name: "summary.md",
              file_asset_ref_id: "file-1",
              file_id: "file-1",
              download_url: "/api/v1/artifacts/artifact-1/download",
              logical_key: "session_file:reports/summary.md",
              version: 1
            },
            version: {
              id: "version-1",
              user_id: "user-1",
              artifact_id: "artifact-1",
              version: 1,
              file_asset_ref_id: "file-1",
              created_at: "2026-07-09T00:00:00.000Z"
            }
          };
        }
      }
    });

    expect(calls).toEqual([{
      user_id: "user-1",
      workspace_id: "workspace-1",
      session_id: "session-1",
      run_id: "run-1",
      path: "reports/summary.md",
      source_path: join("/tmp/session", "reports/summary.md"),
      tool_call_id: "tool-1"
    }]);
    expect(emitted).toHaveLength(1);
    expect(emitted[0]).toMatchObject({
      type: EventType.CUSTOM,
      name: "artifact",
      value: {
        id: "artifact-1",
        file_id: "file-1"
      }
    });
  });

  it("skips failed metadata and non-output paths", async () => {
    const emitted: unknown[] = [];
    const calls: unknown[] = [];
    const sessionOutputService = {
      upsertFromSessionFile: async (input: unknown) => {
        calls.push(input);
        return null;
      }
    };

    await maybeIngestSessionFileOutput({
      metadata: { toolName: "write_file", status: "failed", path: "reports/summary.md" },
      sessionDir: "/tmp/session",
      runContext,
      emitter: { emit: (event) => emitted.push(event) },
      sessionOutputService
    });
    await maybeIngestSessionFileOutput({
      metadata: { toolName: "edit_file", path: "analysis.py" },
      sessionDir: "/tmp/session",
      runContext,
      emitter: { emit: (event) => emitted.push(event) },
      sessionOutputService
    });

    expect(calls).toHaveLength(1);
    expect(emitted).toHaveLength(0);
  });

  it("ingests from write_file tool args when workspace.metadata is missing", async () => {
    const emitted: unknown[] = [];
    const calls: unknown[] = [];

    await maybeIngestSessionFileToolResult({
      toolName: "write_file",
      toolCallId: "call-write-1",
      toolInput: { path: "order_analysis_report.md", content: "# report" },
      rawResult: { observation: "Wrote 3481 bytes to order_analysis_report.md" },
      sessionDir: "/tmp/session",
      runContext,
      emitter: { emit: (event) => emitted.push(event) },
      sessionOutputService: {
        upsertFromSessionFile: async (input) => {
          calls.push(input);
          return {
            artifact: {
              id: "artifact-report",
              type: "markdown",
              name: "order_analysis_report.md",
              file_asset_ref_id: "file-report",
              file_id: "file-report",
              download_url: "/api/v1/artifacts/artifact-report/download",
              logical_key: "session_file:order_analysis_report.md",
              version: 1
            },
            version: {
              id: "version-report",
              user_id: "user-1",
              artifact_id: "artifact-report",
              version: 1,
              file_asset_ref_id: "file-report",
              created_at: "2026-07-09T00:00:00.000Z"
            }
          };
        }
      }
    });

    expect(calls).toEqual([{
      user_id: "user-1",
      workspace_id: "workspace-1",
      session_id: "session-1",
      run_id: "run-1",
      path: "order_analysis_report.md",
      source_path: join("/tmp/session", "order_analysis_report.md"),
      tool_call_id: "call-write-1"
    }]);
    expect(emitted).toHaveLength(1);
    expect(emitted[0]).toMatchObject({
      type: EventType.CUSTOM,
      name: "artifact",
      value: { id: "artifact-report" }
    });
  });

  it("parses path from observation text when tool args are missing", async () => {
    const calls: unknown[] = [];
    await maybeIngestSessionFileToolResult({
      toolName: "write_file",
      rawResult: "Wrote 12 bytes to reports/summary.md",
      sessionDir: "/tmp/session",
      runContext,
      emitter: { emit: () => undefined },
      sessionOutputService: {
        upsertFromSessionFile: async (input) => {
          calls.push(input);
          return null;
        }
      }
    });
    expect(calls).toEqual([
      expect.objectContaining({ path: "reports/summary.md" })
    ]);
  });
});
