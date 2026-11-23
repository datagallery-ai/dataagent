import { describe, expect, it, vi } from "vitest";
import {
  uploadAndPromoteWorkspaceFiles,
  WorkspaceUploadPromoteError,
} from "../workspace-file-upload";

describe("uploadAndPromoteWorkspaceFiles", () => {
  it("requires an active session id", async () => {
    await expect(
      uploadAndPromoteWorkspaceFiles(
        {
          uploadWorkspaceFiles: vi.fn(),
          promoteWorkspaceFile: vi.fn(),
        },
        [new File(["hi"], "a.txt")],
        null,
      ),
    ).rejects.toThrow(/chat session/i);
  });

  it("uploads with session id then promotes each returned file", async () => {
    const uploadWorkspaceFiles = vi.fn().mockResolvedValue({
      files: [
        { id: "ref-1", filename: "a.txt" },
        { id: "ref-2", filename: "b.txt" },
      ],
    });
    const promoteWorkspaceFile = vi
      .fn()
      .mockImplementation(async (id: string) => ({
        id: `ws-${id}`,
        filename: id === "ref-1" ? "a.txt" : "b.txt",
        source: "workspace",
        sessionId: undefined,
      }));

    const result = await uploadAndPromoteWorkspaceFiles(
      { uploadWorkspaceFiles, promoteWorkspaceFile },
      [new File(["a"], "a.txt"), new File(["b"], "b.txt")],
      "session-1",
    );

    expect(uploadWorkspaceFiles).toHaveBeenCalledWith(
      expect.arrayContaining([expect.any(File), expect.any(File)]),
      "session-1",
    );
    expect(promoteWorkspaceFile).toHaveBeenCalledTimes(2);
    expect(promoteWorkspaceFile).toHaveBeenNthCalledWith(1, "ref-1");
    expect(promoteWorkspaceFile).toHaveBeenNthCalledWith(2, "ref-2");
    expect(result.uploaded.map((file) => file.id)).toEqual(["ref-1", "ref-2"]);
    expect(result.promoted.map((file) => file.id)).toEqual(["ws-ref-1", "ws-ref-2"]);
    expect(result.failed).toEqual([]);
  });

  it("summarizes partial promote failure when the second promote rejects", async () => {
    const uploadWorkspaceFiles = vi.fn().mockResolvedValue({
      files: [
        { id: "ref-1", filename: "a.txt" },
        { id: "ref-2", filename: "b.txt" },
      ],
    });
    const promoteWorkspaceFile = vi
      .fn()
      .mockImplementation(async (id: string) => {
        if (id === "ref-2") {
          throw new Error("PROMOTE_FAILED:disk full");
        }
        return {
          id: `ws-${id}`,
          filename: "a.txt",
          source: "workspace",
          sessionId: undefined,
        };
      });

    let caught: unknown;
    try {
      await uploadAndPromoteWorkspaceFiles(
        { uploadWorkspaceFiles, promoteWorkspaceFile },
        [new File(["a"], "a.txt"), new File(["b"], "b.txt")],
        "session-1",
      );
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(WorkspaceUploadPromoteError);
    const error = caught as WorkspaceUploadPromoteError;
    expect(error.result.uploaded.map((file) => file.id)).toEqual(["ref-1", "ref-2"]);
    expect(error.result.promoted.map((file) => file.id)).toEqual(["ws-ref-1"]);
    expect(error.result.failed).toEqual([
      { id: "ref-2", filename: "b.txt", error: "PROMOTE_FAILED:disk full" },
    ]);
    expect(error.message).toMatch(/partial success/i);
    expect(error.message).toMatch(/promoted 1/i);
    expect(error.message).toMatch(/failed 1/i);
    expect(error.message).toMatch(/b\.txt/);
    expect(error.message).toMatch(/remain in workspace/i);
    expect(promoteWorkspaceFile).toHaveBeenCalledTimes(2);
  });
});
