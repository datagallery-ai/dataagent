import { rmSync } from "node:fs";

import { LocalKnowledgeService } from "../packages/knowledge/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const databasePath = `storage/metadata/knowledge-retrieval-policy-${Date.now()}.sqlite`;

const collectionId = "policy-kb";
const chunkPolicyCollectionId = "chunk-policy-kb";
const store = createMetadataStore({ database_path: databasePath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

const knowledge = new LocalKnowledgeService(store);

try {
  store.configResources.upsert({
    id: collectionId,
    workspace_id: workspaceId,
    user_id: userId,
    kind: "knowledge-base",
    name: "Policy KB",
    payload: { retrievalTopK: 2 },
    default_enabled: true,
    status: "ready"
  });

  await knowledge.ingestText({
    user_id: userId,
    collection_id: collectionId,
    filename: "alpha-1.md",
    content: "alpha revenue metric one"
  });
  await knowledge.ingestText({
    user_id: userId,
    collection_id: collectionId,
    filename: "alpha-2.md",
    content: "alpha revenue metric two"
  });
  await knowledge.ingestText({
    user_id: userId,
    collection_id: collectionId,
    filename: "alpha-3.md",
    content: "alpha revenue metric three"
  });

  const configuredTopK = await knowledge.retrieve({
    user_id: userId,
    collection_id: collectionId,
    query: "alpha revenue metric"
  });
  assert(configuredTopK.length === 2, `retrievalTopK should cap default hits to 2, got ${configuredTopK.length}`);

  const explicitTopK = await knowledge.retrieve({
    user_id: userId,
    collection_id: collectionId,
    query: "alpha revenue metric",
    top_k: 3
  });
  assert(explicitTopK.length === 3, `explicit top_k should override configured default, got ${explicitTopK.length}`);

  const maxScore = Math.max(...explicitTopK.map((chunk) => chunk.score));
  store.configResources.upsert({
    id: collectionId,
    workspace_id: workspaceId,
    user_id: userId,
    kind: "knowledge-base",
    name: "Policy KB",
    payload: { retrievalTopK: 3, scoreThreshold: Math.min(1, maxScore + 0.000001) },
    default_enabled: true,
    status: "ready"
  });
  const filtered = await knowledge.retrieve({
    user_id: userId,
    collection_id: collectionId,
    query: "alpha revenue metric"
  });
  assert(filtered.length === 0, `scoreThreshold should filter low-score chunks, got ${filtered.length}`);

  store.configResources.upsert({
    id: chunkPolicyCollectionId,
    workspace_id: workspaceId,
    user_id: userId,
    kind: "knowledge-base",
    name: "Chunk Policy KB",
    payload: { chunkOverlap: 20, chunkSize: 200 },
    default_enabled: true,
    status: "ready"
  });
  await knowledge.ingestText({
    user_id: userId,
    collection_id: chunkPolicyCollectionId,
    filename: "long.md",
    content: "alpha ".repeat(120)
  });
  const chunkCount = store.db.prepare(`
    SELECT COUNT(*) AS count FROM knowledge_chunks WHERE user_id = ? AND collection_id = ?
  `).get(userId, chunkPolicyCollectionId)?.count;
  assert(typeof chunkCount === "number" && chunkCount > 1, `chunkSize should split long content, got ${chunkCount}`);

  console.log(
    `Knowledge retrieval policy smoke OK: configuredTopK=${configuredTopK.length}, `
      + `explicitTopK=${explicitTopK.length}, chunkCount=${chunkCount}`
  );
} finally {
  store.close();
  rmSync(databasePath, { force: true });
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}
