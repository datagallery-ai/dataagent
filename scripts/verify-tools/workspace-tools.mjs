#!/usr/bin/env node
/**
 * Deterministic runtime verification of Mastra workspace tools.
 * Records direct execution results, data-* chunk emissions, and governed-adapter output.
 */
import { createWorkspaceTools } from "@mastra/core/workspace";
import { rmSync } from "node:fs";
import path from "node:path";

import { createRunWorkspace } from "../../packages/agent-runtime/dist/tools/workspace-factory.js";
import { GovernedToolFactory } from "../../packages/agent-runtime/dist/tools/governed-tool-factory.js";

const { createToolObservationBoundary } = await import(
  "../../packages/agent-runtime/dist/context/tool-observation/tool-observation-boundary.js"
);
const { ToolObservationDispatcher } = await import(
  "../../packages/agent-runtime/dist/context/tool-observation/tool-observation-dispatcher.js"
);

const timestamp = Date.now();
const workspaceRoot = `storage/verify-tools/workspace-${timestamp}`;

const runContext = {
  user_id: "verify-tools-user",
  session_id: "verify-tools-session",
  run_id: `verify-tools-run-${timestamp}`,
  selected_datasource_id: "verify-source",
  enabled_datasource_ids: ["verify-source"],
  user_input: "verify",
  chat_mode: "copilotkit",
  model_name: "verify"
};

const truncate = (value, max = 200) => {
  const s = typeof value === "string" ? value : JSON.stringify(value);
  return s.length <= max ? s : `${s.slice(0, max)}…`;
};

const returnShape = (value) => {
  if (value === null) return "null";
  if (typeof value === "string") return "string";
  if (typeof value === "object") return "json/object";
  return typeof value;
};

const dataChunkTypes = (chunks) =>
  [...new Set(chunks.filter((c) => typeof c?.type === "string" && c.type.startsWith("data-")).map((c) => c.type))];

function makeWriter() {
  const customChunks = [];
  const writer = {
    custom: async (c) => {
      customChunks.push(c);
    },
    write: async (c) => {
      customChunks.push({ write: c });
    }
  };
  return { customChunks, writer };
}

function makeExecCtx({ name, writer, workspace }) {
  return {
    context: { requestContext: new Map() },
    mastra: undefined,
    agentName: "verify",
    name,
    writer,
    agent: { toolCallId: `tc-${name}` },
    workspace
  };
}

async function runTool(tool, name, args, execCtx, customChunks) {
  customChunks.length = 0;
  const result = await tool.execute(args, execCtx);
  return {
    ok: true,
    result,
    shape: returnShape(result),
    sample: truncate(result),
    dataTypes: dataChunkTypes(customChunks)
  };
}

async function runToolExpectThrow(tool, name, args, execCtx, customChunks) {
  customChunks.length = 0;
  try {
    await tool.execute(args, execCtx);
    return { ok: false, threw: false, error: "expected throw but succeeded" };
  } catch (error) {
    return {
      ok: true,
      threw: true,
      error: truncate(error instanceof Error ? error.message : String(error))
    };
  }
}

function buildGovernedTool(rawTool, toolName) {
  const boundary = createToolObservationBoundary({
    identity: {
      resourceId: runContext.user_id,
      runId: runContext.run_id,
      sessionId: runContext.session_id
    }
  });
  const dispatcher = new ToolObservationDispatcher(boundary.packager, {
    modelName: runContext.model_name,
    resourceId: runContext.user_id,
    runId: runContext.run_id,
    sessionId: runContext.session_id
  });
  const factory = new GovernedToolFactory(dispatcher);
  return factory.governTool(toolName, rawTool);
}

const results = [];
let runWorkspace;
let workspaceRootAbs;

try {
  runWorkspace = createRunWorkspace({ runContext, workspaceRoot });
  workspaceRootAbs = path.resolve(workspaceRoot);

  console.log(
    JSON.stringify({
      phase: "setup",
      workspaceRoot: workspaceRootAbs,
      runDir: runWorkspace.runDir,
      isolation: runWorkspace.isolation,
      commandExecutionEnabled: runWorkspace.commandExecutionEnabled,
      pythonRuntime: runWorkspace.pythonRuntime?.venvRoot ?? null
    })
  );

  await runWorkspace.workspace.init();
  const toolsRaw = await createWorkspaceTools(runWorkspace.workspace, {
    requestContext: {},
    workspace: runWorkspace.workspace
  });

  // Workspace file tools no longer auto-record artifacts; verify the raw tools directly.
  const tools = toolsRaw;

  const testFile = "verify-test.txt";
  const testDir = "verify-subdir";
  const initialContent = "hello-verify-tools\nline-two\n";
  const editedContent = "hello-verify-tools\nline-two-edited\n";

  // --- mkdir ---
  {
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "mkdir", writer, workspace: runWorkspace.workspace });
    const r = await runTool(tools.mkdir, "mkdir", { path: testDir }, execCtx, customChunks);
    results.push({ tool: "mkdir", ...r, notes: "" });
  }

  // --- write_file (new file) ---
  {
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "write_file", writer, workspace: runWorkspace.workspace });
    const r = await runTool(
      tools.write_file,
      "write_file",
      { path: testFile, content: initialContent },
      execCtx,
      customChunks
    );
    results.push({ tool: "write_file", ...r, notes: "new file" });
  }

  // --- write_file without read on existing (expected throw) ---
  {
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "write_file", writer, workspace: runWorkspace.workspace });
    const r = await runToolExpectThrow(
      tools.write_file,
      "write_file",
      { path: testFile, content: "should-fail" },
      execCtx,
      customChunks
    );
    results.push({
      tool: "write_file (no-read overwrite)",
      ok: r.threw,
      result: null,
      shape: "n/a",
      sample: r.error,
      dataTypes: dataChunkTypes(customChunks),
      notes: "expected throw (requireReadBeforeWrite)"
    });
  }

  // --- read_file ---
  {
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "read_file", writer, workspace: runWorkspace.workspace });
    const r = await runTool(tools.read_file, "read_file", { path: testFile }, execCtx, customChunks);
    results.push({ tool: "read_file", ...r, notes: "" });
  }

  // --- write_file after read (overwrite) ---
  {
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "write_file", writer, workspace: runWorkspace.workspace });
    const r = await runTool(
      tools.write_file,
      "write_file",
      { path: testFile, content: initialContent + "overwrite-ok\n" },
      execCtx,
      customChunks
    );
    results.push({ tool: "write_file (after read)", ...r, notes: "overwrite after read_file" });
  }

  // --- edit_file (requires prior read) ---
  {
    const { customChunks: readChunks, writer: readWriter } = makeWriter();
    const readCtx = makeExecCtx({ name: "read_file", writer: readWriter, workspace: runWorkspace.workspace });
    await tools.read_file.execute({ path: testFile }, readCtx);

    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "edit_file", writer, workspace: runWorkspace.workspace });
    const r = await runTool(
      tools.edit_file,
      "edit_file",
      {
        path: testFile,
        old_string: "line-two",
        new_string: "line-two-edited"
      },
      execCtx,
      customChunks
    );
    results.push({ tool: "edit_file", ...r, notes: "" });
  }

  // --- file_stat ---
  {
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "file_stat", writer, workspace: runWorkspace.workspace });
    const r = await runTool(tools.file_stat, "file_stat", { path: testFile }, execCtx, customChunks);
    results.push({ tool: "file_stat", ...r, notes: "" });
  }

  // --- list_files ---
  {
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "list_files", writer, workspace: runWorkspace.workspace });
    const r = await runTool(tools.list_files, "list_files", { path: "." }, execCtx, customChunks);
    results.push({ tool: "list_files", ...r, notes: "" });
  }

  // --- grep ---
  {
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "grep", writer, workspace: runWorkspace.workspace });
    const r = await runTool(
      tools.grep,
      "grep",
      { pattern: "verify", path: testFile },
      execCtx,
      customChunks
    );
    results.push({ tool: "grep", ...r, notes: "" });
  }

  // --- execute_command ---
  {
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "execute_command", writer, workspace: runWorkspace.workspace });
    if (runWorkspace.commandExecutionEnabled) {
      const r = await runTool(
        tools.execute_command,
        "execute_command",
        { command: "echo verify-ok" },
        execCtx,
        customChunks
      );
      results.push({
        tool: "execute_command",
        ...r,
        notes: `isolation=${runWorkspace.isolation}`
      });

      if (runWorkspace.pythonRuntime) {
        const py = await runTool(
          tools.execute_command,
          "execute_command",
          {
            command:
              "python3.12 -c \"import numpy, pandas, matplotlib, sklearn; print('ml-ok', numpy.__version__)\""
          },
          execCtx,
          customChunks
        );
        results.push({
          tool: "execute_command (python ml)",
          ...py,
          notes: `venv=${runWorkspace.pythonRuntime.venvRoot}`
        });
      }
    } else {
      results.push({
        tool: "execute_command",
        ok: false,
        result: null,
        shape: "n/a",
        sample: "commandExecutionEnabled=false",
        dataTypes: [],
        notes: "disabled in environment"
      });
    }
  }

  // --- BONUS: governed adapter output ---
  const governed = {};
  {
    const governedPath = "governed-new.txt";
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "write_file", writer, workspace: runWorkspace.workspace });
    const governedWrite = buildGovernedTool(tools.write_file, "write_file");
    const rawWrite = await tools.write_file.execute(
      { path: governedPath, content: "governed-test" },
      execCtx
    );
    const governedOut = await governedWrite.execute(
      { path: "governed-governed.txt", content: "governed-test" },
      execCtx
    );
    governed.write_file = {
      rawSample: truncate(rawWrite),
      rawShape: returnShape(rawWrite),
      governedModel: governedOut,
      governedShape: returnShape(governedOut),
      governedSample: truncate(governedOut)
    };
  }
  {
    const { customChunks, writer } = makeWriter();
    const execCtx = makeExecCtx({ name: "read_file", writer, workspace: runWorkspace.workspace });
    const governedRead = buildGovernedTool(tools.read_file, "read_file");
    const rawRead = await tools.read_file.execute({ path: testFile }, execCtx);
    const governedOut = await governedRead.execute({ path: testFile }, execCtx);
    governed.read_file = {
      rawSample: truncate(rawRead),
      rawShape: returnShape(rawRead),
      governedModel: governedOut,
      governedShape: returnShape(governedOut),
      governedSample: truncate(governedOut)
    };
  }

  await runWorkspace.workspace.destroy();

  const emitters = results.filter((r) => r.dataTypes?.length > 0);

  console.log("\n=== VERIFICATION REPORT ===\n");
  console.log(
    `Environment: isolation=${runWorkspace.isolation}, commandExecutionEnabled=${runWorkspace.commandExecutionEnabled}`
  );
  console.log(`Workspace: ${workspaceRootAbs}\n`);

  console.log("| tool | direct-exec OK? | return shape + sample | emits data-* chunks? | notes |");
  console.log("|------|-----------------|----------------------|----------------------|-------|");
  for (const r of results) {
    const ok = r.ok ? "yes" : "no";
    const shapeSample = `${r.shape}: ${r.sample ?? ""}`;
    const dataTypes = r.dataTypes?.length ? r.dataTypes.join(", ") : "none";
    console.log(`| ${r.tool} | ${ok} | ${shapeSample.replace(/\|/g, "\\|")} | ${dataTypes} | ${r.notes ?? ""} |`);
  }

  console.log("\n=== DATA-* CHUNK EMITTERS (stream-crash risk via @ag-ui/mastra) ===");
  if (emitters.length === 0) {
    console.log("None of the tested tools emitted data-* chunks via writer.custom.");
  } else {
    for (const r of emitters) {
      console.log(`- ${r.tool}: ${r.dataTypes.join(", ")}`);
    }
  }

  console.log("\n=== GOVERNED ADAPTER BONUS ===");
  console.log(JSON.stringify(governed, null, 2));
  console.log(
    [
      "\nAdapter contract: pickFields/asRecord adapters expect JSON objects with named fields;",
      'plain-string returns yield empty {} (pickFields) or { value: "..." } (asRecord on string).'
    ].join(" ")
  );

  process.exitCode =
    process.exitCode === 1
      ? 1
      : results.some((r) => !r.ok && !r.tool.includes("no-read"))
        ? 1
        : 0;
} catch (error) {
  console.error("FATAL:", error);
  process.exitCode = 1;
} finally {
  if (workspaceRootAbs) {
    rmSync(workspaceRootAbs, { force: true, recursive: true });
    console.log(`\nCleaned up: ${workspaceRootAbs}`);
  }
}
