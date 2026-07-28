/** Verify semantic trace sections with the configured real LLM provider. */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { EventType } from "@ag-ui/client";
import { createModelProviderFromEnv } from "../packages/agent-runtime/dist/index.js";
import {
  createMetadataStore,
  runEventRecordToEnvelope,
} from "../packages/metadata/dist/index.js";
import { TraceSectionCoordinator } from "../apps/api/dist/trace-section-coordinator.js";
import { buildSessionTraceDag } from "../apps/api/dist/trace-dag.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

loadDotEnv();

const modelProvider = createModelProviderFromEnv(process.env);
if (modelProvider.kind === "mock") {
  throw new Error("LLM_API_KEY is required for smoke:trace-sections.");
}

const stamp = Date.now();

const sessionId = `trace-section-session-${stamp}`;
const runId = `trace-section-run-${stamp}`;
const store = createMetadataStore({ database_path: `storage/trace-sections/${stamp}/metadata.sqlite` });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;


try {
  store.sessions.create({ user_id: userId, id: sessionId, title: "Trace section smoke" });
  store.runs.create({
    user_id: userId,
    id: runId,
    session_id: sessionId,
    user_input: "Inspect the orders table, validate a revenue query, and report the result.",
    status: "running"
  });
  store.conversationMessages.append({
    user_id: userId,
    session_id: sessionId,
    run_id: runId,
    id: `message-${stamp}`,
    role: "user",
    source: "client",
    content_text: "Inspect the orders table, validate a revenue query, and report the result."
  });

  const coordinator = new TraceSectionCoordinator(store, modelProvider, userId);
  const snapshot = store.contextPackageSnapshots.create({
    user_id: userId,
    session_id: sessionId,
    run_id: runId,
    package_id: `package-${stamp}`,
    revision: 1,
    payload: { packageId: `package-${stamp}`, revision: 1 }
  });

  const basePhases = [
    ["inspect_schema", { table_names: ["orders"] }, { columns: ["id", "category", "gmv"] }],
    ["run_sql_readonly", { sql: "SELECT COUNT(*) FROM orders" }, { rows: [[18_250]] }],
    ["run_sql_readonly", { sql: "SELECT MIN(gmv), MAX(gmv) FROM orders" }, { rows: [[-7.07, 4_932.19]] }],
    ["run_sql_readonly", { sql: "SELECT percentile(gmv, 0.1) FROM orders" }, { rows: [[56.98]] }],
    ["run_sql_readonly", { sql: "SELECT category, AVG(gmv) FROM orders GROUP BY category" }, { row_count: 8 }],
    ["run_sql_readonly", { sql: "SELECT region, AVG(gmv) FROM orders GROUP BY region" }, { row_count: 6 }],
    ["run_sql_readonly", { sql: "SELECT age_band, AVG(gmv) FROM orders GROUP BY age_band" }, { row_count: 5 }],
    ["execute_command", { command: "calculate winsorized metrics" }, { exit_code: 0 }],
    ["execute_command", { command: "simulate freight reduction" }, { exit_code: 0 }],
    ["write_file", { path: "report.md" }, { bytes_written: 2_400 }]
  ];
  const phases = [...basePhases, ...basePhases];
  const events = phases.flatMap(([toolName, input, output], index) => {
    const toolCallId = `phase-call-${index + 1}`;
    return [
      contextCompiledEvent(index + 1),
      toolEvent(EventType.TOOL_CALL_START, toolName, toolCallId, input),
      toolEvent(EventType.TOOL_CALL_RESULT, toolName, toolCallId, output)
    ];
  });
  events.push({ type: EventType.RUN_FINISHED, threadId: sessionId, runId });

  let terminalEventSeq = 0;
  for (const event of events) {
    const record = store.runEvents.append({ user_id: userId, run_id: runId, session_id: sessionId, event });
    if (event.type === EventType.CUSTOM && event.name === "context.compiled") {
      store.checkpoints.create({
        id: `checkpoint-${record.seq}`,
        user_id: userId,
        session_id: sessionId,
        run_id: runId,
        event_seq: record.seq,
        context_package_id: snapshot.id,
        context_package_revision: snapshot.revision,
        kind: "context-compiled",
        status: "stable",
        label: `Context step ${event.value.step_number}`
      });
    }
    coordinator.observe(runEventRecordToEnvelope(record));
    if (event.type === EventType.RUN_FINISHED) {
      terminalEventSeq = record.seq;
    }
  }

  const sections = await waitForSections(store, userId, runId, terminalEventSeq);
  const dag = buildSessionTraceDag({ metadataStore: store, sessionId, userId });
  assert(dag.sections.length === sections.length, "Trace DAG did not include every persisted section.");
  assert(dag.sections.length >= 2, "Twenty context steps should produce multiple semantic sections.");
  dag.sections.forEach((section) => {
    const contextCount = store.checkpoints
      .listByRun({ user_id: userId, run_id: runId })
      .filter((checkpoint) => checkpoint.kind === "context-compiled"
        && checkpoint.event_seq >= section.startEventSeq
        && checkpoint.event_seq <= section.endEventSeq)
      .length;
    assert(section.status === "completed", "Terminal run should complete every section.");
    assert(contextCount > 0 && contextCount <= 16, `Section contains ${contextCount} context steps.`);
    assert(section.nodeIds.length > 0, "Section should include trace nodes.");
    assert(section.title.length > 0 && section.summary.length > 0, "LLM section title or summary was empty.");
    assert(!section.title.startsWith("Trace steps"), "Trace section fell back instead of using the real LLM response.");
  });
  assert(sections.every((section) => !section.phase_key.startsWith("legacy:")), "Phase keys should be persisted.");
  const terminalNode = dag.nodes.find((node) => node.kind === "run-terminal");
  const finalSection = dag.sections.at(-1);
  assert(
    terminalNode && finalSection?.nodeIds.includes(terminalNode.id),
    "Final section should contain the terminal node.",
  );
  console.log(`Trace section smoke passed with ${modelProvider.model_name}: ${dag.sections.length} sections`);
} finally {
  store.close();
}

// Mastra may retain an idle telemetry/socket handle after a successful real-model call.
process.exit(0);

function contextCompiledEvent(stepNumber) {
  return {
    type: EventType.CUSTOM,
    name: "context.compiled",
    value: { package_id: "trace-package", package_revision: 1, step_number: stepNumber }
  };
}

function toolEvent(type, toolCallName, toolCallId, payload) {
  return {
    type,
    toolCallName,
    toolCallId,
    ...(type === EventType.TOOL_CALL_RESULT ? { content: JSON.stringify(payload) } : { input: JSON.stringify(payload) })
  };
}

async function waitForSections(store, userId, runId, terminalEventSeq) {
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    const sections = store.traceSections.listByRun({ user_id: userId, run_id: runId });
    const latest = sections.at(-1);
    if (sections.length > 0 && latest?.end_event_seq >= terminalEventSeq
      && sections.every((section) => section.status === "completed")) {
      return sections;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("Timed out waiting for the real LLM trace section summaries.");
}

function loadDotEnv() {
  try {
    const source = readFileSync(join(process.cwd(), ".env"), "utf8");
    for (const line of source.split("\n")) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) continue;
      const separator = trimmed.indexOf("=");
      if (separator < 1) continue;
      const key = trimmed.slice(0, separator).trim();
      const value = trimmed.slice(separator + 1).trim();
      if (!process.env[key]) process.env[key] = value;
    }
  } catch {
    // The caller may supply provider configuration through the process environment.
  }
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}
