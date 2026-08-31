import { LocalKnowledgeService } from "../packages/knowledge/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { rmSync } from "node:fs";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const databasePath = `storage/metadata/memory-recall-shadow-${Date.now()}.sqlite`;

const sessionId = "memory-recall-shadow-session";
const runId = "memory-recall-shadow-run";
const datasourceId = "api-duckdb-demo";
const collectionId = "memory-shadow-kb";
const query = "GMV refund rate orders";
const store = createMetadataStore({ database_path: databasePath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

const knowledge = new LocalKnowledgeService(store);

try {
  store.sessions.create({
    user_id: userId,
    id: sessionId,
    title: "memory recall shadow",
    selected_datasource_id: datasourceId
  });
  store.runs.create({
    user_id: userId,
    id: runId,
    session_id: sessionId,
    request_fingerprint: "memory-recall-shadow",
    user_input: query,
    status: "running",
    datasource_id: datasourceId
  });
  store.longTermMemories.upsert({
    id: "shadow-memory-user",
    user_id: userId,
    scope: "user",
    kind: "user_preference",
    content_text: "用户分析 GMV 时希望默认同时关注 refund rate。",
    confidence: 0.9,
    source_run_id: runId
  });
  store.longTermMemories.upsert({
    id: "shadow-memory-datasource",
    user_id: userId,
    scope: "datasource",
    datasource_id: datasourceId,
    kind: "analysis_finding",
    content_text: "orders 表适合按 category 汇总 GMV 并补充退款率分析。",
    confidence: 0.8,
    source_run_id: runId
  });
  await knowledge.ingestText({
    user_id: userId,
    collection_id: collectionId,
    filename: "orders-analysis-notes.md",
    content: [
      "orders 表包含订单金额、类目和退款相关字段。",
      "分析 GMV 时可以同时计算 refund rate。",
      "这些知识库内容来自用户上传资料，不等同于长期对话记忆。"
    ].join("\n")
  });

  const memoryHits = store.longTermMemories.listRelevant({
    user_id: userId,
    session_id: sessionId,
    datasource_id: datasourceId,
    query,
    limit: 5
  });
  const knowledgeHits = await knowledge.retrieve({
    user_id: userId,
    collection_id: collectionId,
    query,
    top_k: 5
  });
  const report = {
    query,
    localLongTermMemory: {
      count: memoryHits.length,
      ids: memoryHits.map((memory) => memory.id)
    },
    knowledge: {
      count: knowledgeHits.length,
      chunkIds: knowledgeHits.map((chunk) => chunk.chunk_id)
    },
    mastraSemanticRecall: {
      reason: "Vector store and embedder are not enabled for production memory recall.",
      status: "not_configured"
    }
  };

  if (report.localLongTermMemory.count < 2) {
    throw new Error("Expected local long-term memory hits in shadow report");
  }
  if (report.knowledge.count < 1) {
    throw new Error("Expected Knowledge hits in shadow report");
  }

  console.log(
    `Memory recall shadow smoke OK: ltm=${report.localLongTermMemory.count}, knowledge=${report.knowledge.count}, ` +
      `mastra=${report.mastraSemanticRecall.status}`
  );
} finally {
  store.close();
  rmSync(databasePath, { force: true });
}
