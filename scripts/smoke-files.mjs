import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { LocalArtifactService } from "../packages/artifacts/dist/index.js";
import { fileAssetRefDto, LocalFileAssetService } from "../packages/files/dist/index.js";
import { LocalKnowledgeService } from "../packages/knowledge/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const root = mkdtempSync(join(tmpdir(), "open-data-foundry-files-smoke-"));
const store = createMetadataStore({
  database_path: join(root, "metadata.sqlite")
});
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

const files = new LocalFileAssetService(store, { storageRoot: join(root, "files") });
const artifacts = new LocalArtifactService(store, files);
const knowledge = new LocalKnowledgeService(store);

const sessionId = "files-smoke-session";
const runId = "files-smoke-run";

try {
  store.sessions.create({ user_id: userId, id: sessionId, title: "files smoke" });
  store.runs.create({
    user_id: userId,
    id: runId,
    session_id: sessionId,
    user_input: "files smoke",
    status: "running"
  });

  const first = files.createRef({
    user_id: userId,
    workspace_id: workspaceId,
    filename: "orders.csv",
    declared_mime_type: "text/csv; charset=utf-8",
    source: "upload",
    content: Buffer.from("id,total\n1,42\n", "utf8")
  });
  const duplicate = files.createRef({
    user_id: userId,
    workspace_id: workspaceId,
    filename: "orders-copy.csv",
    declared_mime_type: "text/csv; charset=utf-8",
    source: "upload",
    content: Buffer.from("id,total\n1,42\n", "utf8")
  });
  assert.equal(first.asset.id, duplicate.asset.id, "duplicate content should reuse the same FileAsset");
  assert.notEqual(first.ref.id, duplicate.ref.id, "duplicate uploads should still create distinct refs");

  const downloaded = files.readRef({ user_id: userId, workspace_id: workspaceId, id: first.ref.id });
  assert.equal(downloaded.body.toString("utf8"), "id,total\n1,42\n");

  const materializedPath = join(root, "workspace", "input", "orders.csv");
  files.materializeRefToPath({ ref: first.ref, targetPath: materializedPath });
  assert.equal(readFileSync(materializedPath, "utf8"), "id,total\n1,42\n");

  const artifactSource = join(root, "workspace", "output", "report.md");
  files.createRefFromPath({
    user_id: userId,
    workspace_id: workspaceId,
    session_id: sessionId,
    run_id: runId,
    filename: "source-report.md",
    declared_mime_type: "text/markdown; charset=utf-8",
    source: "workspace",
    path: materializedPath
  });
  const reportContent = Buffer.from("# Report\n\nRevenue is 42.\n", "utf8");
  const reportSeed = files.createRef({
    user_id: userId,
    workspace_id: workspaceId,
    session_id: sessionId,
    run_id: runId,
    filename: "report-seed.md",
    declared_mime_type: "text/markdown; charset=utf-8",
    source: "workspace",
    content: reportContent
  });
  files.materializeRefToPath({ ref: reportSeed.ref, targetPath: artifactSource, linkStrategy: "copy" });
  const artifact = await artifacts.createArtifactFromFile({
    user_id: userId,
    session_id: sessionId,
    run_id: runId,
    workspace_id: workspaceId,
    type: "markdown",
    name: "report.md",
    source_path: artifactSource,
    preview_json: { title: "Report" }
  });
  assert.equal(typeof artifact.file_id, "string");
  assert.equal(artifact.download_url, `/api/v1/artifacts/${artifact.id}/download`);

  // R-015: backend rule-based chart artifact (no agent tool). createChartArtifact
  // validates + normalizes the preview_json to the {chartType,unit?,points,series?}
  // contract so the frontend can render bar/line/pie reliably.
  const chartArtifact = await artifacts.createChartArtifact({
    user_id: userId,
    session_id: sessionId,
    run_id: runId,
    name: "渠道订单量",
    chartType: "bar",
    unit: "单",
    points: [
      { label: "search", value: 42 },
      { label: "direct", value: 18 },
      { label: "bad", value: NaN },          // dropped: non-finite value
      { label: "", value: 5 }                // dropped: empty label
    ],
    metadata_json: { source_artifact_id: artifact.id, tool_call_id: "tool-chart-1" }
  });
  assert.equal(chartArtifact.type, "chart");
  assert.equal(typeof chartArtifact.id, "string");
  const chartRecord = store.artifacts.get({ user_id: userId, artifact_id: chartArtifact.id });
  const chartPreview = JSON.parse(chartRecord.preview_json);
  assert.equal(chartPreview.chartType, "bar", "chart preview should carry chartType");
  assert.equal(chartPreview.unit, "单", "chart preview should carry unit");
  assert.equal(chartPreview.points.length, 2, "malformed points should be dropped, leaving 2");
  assert.equal(chartPreview.points[0].label, "search");
  assert.equal(chartPreview.points[0].value, 42);
  const chartMeta = JSON.parse(chartRecord.metadata_json);
  assert.equal(chartMeta.source_artifact_id, artifact.id, "chart metadata_json should carry source_artifact_id");
  assert.equal(chartMeta.tool_call_id, "tool-chart-1", "chart metadata_json should carry tool_call_id");
  assert.equal(chartArtifact.tool_call_id, "tool-chart-1", "chart summary should expose tool_call_id");
  // multi-series variant
  const seriesChart = await artifacts.createChartArtifact({
    user_id: userId,
    session_id: sessionId,
    run_id: runId,
    name: "GMV trend",
    chartType: "line",
    unit: "元",
    series: [{ name: "GMV", points: [{ label: "2026-06-01", value: 1200 }] }]
  });
  const seriesPreview = JSON.parse(store.artifacts.get({ user_id: userId, artifact_id: seriesChart.id }).preview_json);
  assert.equal(seriesPreview.series.length, 1, "multi-series chart preview should carry series[]");
  assert.equal(seriesPreview.series[0].name, "GMV");
  // empty chart data is rejected
  let chartError;
  try {
    await artifacts.createChartArtifact({
      user_id: userId, session_id: sessionId, run_id: runId, name: "empty", chartType: "pie"
    });
  } catch (error) {
    chartError = error;
  }
  assert(Boolean(chartError), "createChartArtifact with no points/series should throw CHART_DATA_REQUIRED");

  const document = await knowledge.ingestText({
    user_id: userId,
    collection_id: "files-smoke-kb",
    filename: first.ref.filename,
    content: downloaded.body.toString("utf8"),
    file_asset_ref_id: first.ref.id,
    mime_type: downloaded.mimeType
  });
  assert.equal(document.file_asset_ref_id, first.ref.id);
  assert.equal(document.status, "ready");
  assert.equal(existsSync(first.asset.storage_path), true);

  // R-021: unified filtering + derived scope/origin tags.
  const allRefs = files.listRefs({ user_id: userId, workspace_id: workspaceId, limit: 500 });
  // 2 upload (no session) + 2 workspace (with session) + 1 knowledge = 5
  assert.equal(allRefs.length, 5, `expected 5 refs total, got ${allRefs.length}`);
  // DTO scope/origin derivation
  const byId = new Map(allRefs.map((r) => [r.ref.id, r]));
  // scope=workspace ⇒ session_id IS NULL
  const workspaceScoped = files.listRefs({ user_id: userId, workspace_id: workspaceId, limit: 500, session_id: null });
  assert.equal(workspaceScoped.length, 2, `expected 2 cross-session refs, got ${workspaceScoped.length}`);
  assert.ok(workspaceScoped.every((r) => r.ref.source === "upload"), "cross-session refs should be uploads");
  // scope=session ⇒ session_id IS NOT NULL
  const sessionScoped = files.listRefs({ user_id: userId, workspace_id: workspaceId, limit: 500, has_session: true });
  assert.equal(sessionScoped.length, 3, `expected 3 session refs, got ${sessionScoped.length}`);
  // sessionId filter
  const oneSession = files.listRefs({ user_id: userId, workspace_id: workspaceId, limit: 500, session_id: sessionId });
  assert.equal(oneSession.length, 3, `expected 3 refs for session, got ${oneSession.length}`);
  // origin→source: saved maps to workspace (2)
  const savedRefs = files.listRefs({ user_id: userId, workspace_id: workspaceId, limit: 500, source: "workspace" });
  assert.equal(savedRefs.length, 2, `expected 2 workspace-source refs, got ${savedRefs.length}`);
  // DTO derived tags: upload ref → origin=uploaded, scope=workspace
  const uploadDto = fileAssetRefDto(byId.get(first.ref.id));
  assert.equal(uploadDto.origin, "uploaded", "upload ref origin should be 'uploaded'");
  assert.equal(uploadDto.scope, "workspace", "upload ref scope should be 'workspace'");
  // workspace ref with session → origin=saved, scope=session
  const workspaceWithSession = sessionScoped.find((r) => r.ref.source === "workspace");
  const savedDto = fileAssetRefDto(workspaceWithSession);
  assert.equal(savedDto.origin, "saved", "workspace ref origin should be 'saved'");
  assert.equal(savedDto.scope, "session", "workspace ref with session scope should be 'session'");

  // R-022: promote a file-type artifact into a cross-session workspace asset (idempotent).
  const artifactFile = files.getRef({ user_id: userId, workspace_id: workspaceId, id: artifact.file_id });
  const promoted = files.promoteFileToWorkspace({
    user_id: userId,
    workspace_id: workspaceId,
    file_asset_ref_id: artifact.file_id,
    filename: artifact.name
  });
  assert.equal(promoted.ref.source, "workspace", "promoted ref should be source=workspace");
  assert.equal(promoted.ref.session_id, undefined, "promoted ref should have no session_id (cross-session)");
  // Same asset as the artifact's backing file — no byte copy.
  assert.equal(promoted.asset.id, artifactFile.asset.id, "promoted ref should point at the same asset (no copy)");
  // Idempotent: promoting again returns the same ref id.
  const promotedAgain = files.promoteFileToWorkspace({
    user_id: userId,
    workspace_id: workspaceId,
    file_asset_ref_id: artifact.file_id,
    filename: artifact.name
  });
  assert.equal(promotedAgain.ref.id, promoted.ref.id, "re-promote should be idempotent (same ref id)");

  // R-023: list artifacts for a session (session-restore).
  const sessionArtifacts = store.artifacts.listBySession({ user_id: userId, session_id: sessionId });
  // markdown file artifact + 2 chart artifacts (single-series + multi-series) = 3
  assert.equal(sessionArtifacts.length, 3, `expected 3 session artifacts, got ${sessionArtifacts.length}`);
  assert.ok(sessionArtifacts.some((a) => a.id === artifact.id), "file artifact should be in session list");
  assert.ok(sessionArtifacts.some((a) => a.type === "chart"), "chart artifacts should be in session list");
  // empty session returns []
  const emptySession = store.artifacts.listBySession({ user_id: userId, session_id: "no-such-session" });
  assert.equal(emptySession.length, 0, "non-existent session should return empty list");

  console.log(
    `Files smoke OK: asset=${first.asset.id}, refs=5, workspace-scoped=2, session-scoped=3, artifact=${artifact.id}, document=${document.id}, promoted=${promoted.ref.id}`
  );
} finally {
  store.close();
  rmSync(root, { force: true, recursive: true });
}
