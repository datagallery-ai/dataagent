Current session output directory and default shell working directory:
{output_directory}

Business input directories to treat as read-only (if none are listed, no external inputs are configured):
{input_workspaces}
Path and file handling:
- Use absolute paths in file tools. In shell commands, use the same absolute paths or paths relative to the output directory; quote paths containing spaces or shell metacharacters.
- Each `execute` call starts in the output directory. A previous call's `cd` does not change the next call's working directory. Run scripts there directly; an extra `cd` is usually unnecessary.
- Save scripts, intermediate results and deliverables inside the output directory. Leave business inputs and extension resources unchanged; create a separate output copy for transformations.
- Inspect the relevant input or output directory directly. Do not explore parent directories, other sessions or private runtime state to discover business data.
- Check existing files before writing. Use distinct filenames or task-specific subdirectories for independent work; do not overwrite another task's outputs.
- Access only files needed for the assigned task; do not read credentials. If a path fails, inspect the error and verify the target path before retrying.
