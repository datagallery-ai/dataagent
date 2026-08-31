import { BaseToolObservationAdapter, pickFields } from "./base-tool-observation-adapter.js";

/** Project Mastra workspace tool output into a plain string for the model context layer. */
export const projectWorkspaceObservation = (raw: unknown): string => {
  if (typeof raw === "string") {
    return raw.trim().length > 0 ? raw : "(no output)";
  }

  if (raw && typeof raw === "object") {
    const record = raw as Record<string, unknown>;
    if (typeof record.observation === "string") {
      return record.observation.trim().length > 0 ? record.observation : "(no output)";
    }
    try {
      return JSON.stringify(raw);
    } catch {
      return String(raw);
    }
  }

  const text = String(raw ?? "");
  return text.trim().length > 0 ? text : "(no output)";
};

export class ReadFileToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "read_file";
  readonly resultType = "workspace-read-file";

  protected project(raw: unknown): unknown {
    return projectWorkspaceObservation(raw);
  }
}

export class WriteFileToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "write_file";
  readonly resultType = "workspace-write-file";

  protected project(raw: unknown): unknown {
    return projectWorkspaceObservation(raw);
  }
}

export class EditFileToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "edit_file";
  readonly resultType = "workspace-edit-file";

  protected project(raw: unknown): unknown {
    return projectWorkspaceObservation(raw);
  }
}

export class ListFilesToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "list_files";
  readonly resultType = "workspace-list-files";

  protected project(raw: unknown): unknown {
    return projectWorkspaceObservation(raw);
  }
}

export class GrepToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "grep";
  readonly resultType = "workspace-grep";

  protected project(raw: unknown): unknown {
    return projectWorkspaceObservation(raw);
  }
}

export class FileStatToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "file_stat";
  readonly resultType = "workspace-file-stat";

  protected project(raw: unknown): unknown {
    return projectWorkspaceObservation(raw);
  }
}

export class MkdirToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "mkdir";
  readonly resultType = "workspace-mkdir";

  protected project(raw: unknown): unknown {
    return projectWorkspaceObservation(raw);
  }
}

export class ExecuteCommandToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "execute_command";
  readonly resultType = "workspace-execute-command";

  protected project(raw: unknown): unknown {
    return projectWorkspaceObservation(raw);
  }
}

export class PromoteWorkspaceFileToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "promote_workspace_file";
  readonly resultType = "workspace-file-promote";

  protected project(raw: unknown): unknown {
    return pickFields(raw, ["id", "assetId", "filename", "mimeType", "sizeBytes", "sha256", "download_url"]);
  }
}

export class ListWorkspaceFilesToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "list_workspace_files";
  readonly resultType = "workspace-files-list";

  protected project(raw: unknown): unknown {
    return projectWorkspaceObservation(raw);
  }
}

export class ReadWorkspaceFileToolObservationAdapter extends BaseToolObservationAdapter {
  readonly toolName = "read_workspace_file";
  readonly resultType = "workspace-file-read";

  protected project(raw: unknown): unknown {
    return pickFields(raw, ["path", "size_bytes", "mime_type"]);
  }
}
