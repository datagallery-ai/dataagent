import { describe, expect, it } from "vitest";
import { createInitialLiveRun, reduceLiveRunEvent } from "../live-run-state";
import {
  SESSION_OUTPUT_PREVIEW_LIMIT,
  canDownloadArtifact,
  sessionOutputDownload,
  isSessionOutputKey,
  sessionOutputArtifactValue,
  sessionOutputPreviewPaths,
} from "../session-outputs";

const file = (path: string, size = 120) => ({
  path,
  size,
  modifiedAt: "2026-11-09T08:50:04+00:00",
});

describe("session outputs", () => {
  it("previews only small text files, newest first, up to the cap", () => {
    const files = [
      file("reports/summary.md"),
      file("chart.png"),
      file("big.csv", 70 * 1024),
      ...Array.from({ length: SESSION_OUTPUT_PREVIEW_LIMIT + 3 }, (_, i) => file(`notes/${i}.txt`)),
    ];
    const paths = sessionOutputPreviewPaths(files);
    expect(paths[0]).toBe("reports/summary.md");
    expect(paths).not.toContain("chart.png");
    expect(paths).not.toContain("big.csv");
    expect(paths).toHaveLength(SESSION_OUTPUT_PREVIEW_LIMIT);
  });

  it("maps a listed file to a file artifact with inline content", () => {
    const value = sessionOutputArtifactValue(file("reports/channel_roas.md", 395), {
      content: "# Channel ROAS",
    });
    expect(isSessionOutputKey(String(value.logical_key))).toBe(true);

    const state = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "CUSTOM",
      name: "artifact",
      value,
    });
    const artifact = state.artifacts[0];
    expect(artifact?.title).toBe("channel_roas.md");
    expect(artifact?.type).toBe("file");
    expect(artifact?.logicalKey).toBe("session_file:reports/channel_roas.md");
    expect(artifact?.detail).toMatchObject({
      type: "file",
      path: "reports/channel_roas.md",
      size: 395,
      content: "# Channel ROAS",
    });
  });

  it("keeps binary outputs listed without content", () => {
    const value = sessionOutputArtifactValue(file("chart.png", 2048));
    expect(value.preview_json).toEqual({
      path: "chart.png",
      size: 2048,
      mtime: "2026-11-09T08:50:04+00:00",
    });
  });
});

describe("session output downloads", () => {
  it("downloads inline text and hides download for content-less outputs", async () => {
    const withContent = {
      title: "channel_roas.md",
      logicalKey: "session_file:channel_roas.md",
      detail: { type: "file" as const, path: "channel_roas.md", content: "# ROAS" },
    };
    const download = sessionOutputDownload(withContent);
    expect(download?.filename).toBe("channel_roas.md");
    expect(await download?.blob.text()).toBe("# ROAS");
    expect(download?.blob.type).toContain("text/markdown");
    expect(canDownloadArtifact(withContent)).toBe(true);

    const binary = {
      title: "chart.png",
      logicalKey: "session_file:chart.png",
      detail: { type: "file" as const, path: "chart.png" },
    };
    expect(sessionOutputDownload(binary)).toBeNull();
    expect(canDownloadArtifact(binary)).toBe(false);
    expect(canDownloadArtifact({ title: "x", logicalKey: undefined, detail: undefined })).toBe(true);
  });
});
