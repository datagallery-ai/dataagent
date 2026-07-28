import { createMetadataStore, createVerifiedTestIdentity } from "@datafoundry/metadata";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import type { EmbeddingService } from "./embedding-service.js";
import { LocalKnowledgeService } from "./index.js";

const roots: string[] = [];

const embeddingConfig = {
  api_key: "test-key",
  base_url: "https://example.test/v1",
  model: "text-embedding-test",
  provider: "openai-compatible"
};

const createHarness = (embeddingService?: EmbeddingService) => {
  const root = mkdtempSync(join(tmpdir(), "knowledge-lifecycle-"));
  roots.push(root);
  const metadataStore = createMetadataStore({
    database_path: join(root, "metadata.sqlite")
  });
  const { userId, workspaceId } = createVerifiedTestIdentity(metadataStore);
  metadataStore.configResources.upsert({
    id: "kb-1",
    workspace_id: workspaceId,
    user_id: userId,
    kind: "knowledge-base",
    name: "KB",
    payload: {
      embeddingBaseUrl: embeddingConfig.base_url,
      embeddingModel: embeddingConfig.model,
      embeddingProvider: embeddingConfig.provider
    },
    default_enabled: true,
    status: "ready"
  });
  const knowledge = new LocalKnowledgeService(metadataStore, {
    ...(embeddingService
      ? { embedding: embeddingConfig, embeddingService }
      : {})
  });
  return { knowledge, metadataStore, root, userId };
};

afterEach(() => {
  for (const root of roots.splice(0)) {
    rmSync(root, { force: true, recursive: true });
  }
});

describe("LocalKnowledgeService document lifecycle", () => {
  it("hard-deletes a document and cascades chunks, embeddings, and FTS rows", async () => {
    const embed: EmbeddingService = {
      embed: async (texts) => texts.map((_, index) => [index + 1, 0, 0])
    };
    const { knowledge, metadataStore, userId } = createHarness(embed);
    const doc = await knowledge.ingestText({
      user_id: userId,
      collection_id: "kb-1",
      filename: "keep-me.md",
      content: "alpha revenue metric one"
    });
    const other = await knowledge.ingestText({
      user_id: userId,
      collection_id: "kb-1",
      filename: "other.md",
      content: "beta cost metric two"
    });

    const deleted = knowledge.deleteDocument({
      user_id: userId,
      collection_id: "kb-1",
      document_id: doc.id
    });
    expect(deleted).toEqual({ deleted: true, id: doc.id });

    const remaining = knowledge.listDocuments({ user_id: userId, collection_id: "kb-1" });
    expect(remaining.map((item) => item.id)).toEqual([other.id]);

    const chunkCount = metadataStore.db.prepare(
      "SELECT COUNT(*) AS count FROM knowledge_chunks WHERE document_id = ?"
    ).get(doc.id) as { count: number };
    expect(chunkCount.count).toBe(0);

    const embeddingCount = metadataStore.db.prepare(
      "SELECT COUNT(*) AS count FROM knowledge_embeddings WHERE document_id = ?"
    ).get(doc.id) as { count: number };
    expect(embeddingCount.count).toBe(0);

    const hits = await knowledge.retrieve({
      user_id: userId,
      collection_id: "kb-1",
      query: "alpha revenue metric"
    });
    expect(hits.every((hit) => hit.document_id !== doc.id)).toBe(true);
  });

  it("reindexes a failed document and marks it ready on success", async () => {
    let shouldFail = true;
    const embed: EmbeddingService = {
      embed: async (texts) => {
        if (shouldFail) {
          throw new Error("EMBEDDING_REQUEST_FAILED:500:boom");
        }
        return texts.map((_, index) => [index + 1, 0, 0]);
      }
    };
    const { knowledge, metadataStore, userId } = createHarness(embed);

    await expect(knowledge.ingestText({
      user_id: userId,
      collection_id: "kb-1",
      filename: "failed.md",
      content: "alpha revenue metric recoverable"
    })).rejects.toThrow(/EMBEDDING_REQUEST_FAILED/);

    const failed = knowledge.listDocuments({ user_id: userId, collection_id: "kb-1" })[0];
    expect(failed?.status).toBe("failed");

    shouldFail = false;
    const recovered = await knowledge.reindexDocument({
      user_id: userId,
      collection_id: "kb-1",
      document_id: failed!.id
    });
    expect(recovered.status).toBe("ready");

    const embeddingCount = metadataStore.db.prepare(
      "SELECT COUNT(*) AS count FROM knowledge_embeddings WHERE document_id = ?"
    ).get(failed!.id) as { count: number };
    expect(embeddingCount.count).toBeGreaterThan(0);
  });

  it("marks documents ready after a successful collection reindex", async () => {
    let shouldFail = true;
    const embed: EmbeddingService = {
      embed: async (texts) => {
        if (shouldFail) {
          throw new Error("EMBEDDING_REQUEST_FAILED:500:boom");
        }
        return texts.map((_, index) => [index + 1, 0, 0]);
      }
    };
    const { knowledge, userId } = createHarness(embed);
    await expect(knowledge.ingestText({
      user_id: userId,
      collection_id: "kb-1",
      filename: "stuck.md",
      content: "alpha revenue metric stuck"
    })).rejects.toThrow(/EMBEDDING_REQUEST_FAILED/);
    expect(knowledge.listDocuments({ user_id: userId, collection_id: "kb-1" })[0]?.status).toBe("failed");

    shouldFail = false;
    await knowledge.reindex({ user_id: userId, collection_id: "kb-1" });
    expect(knowledge.listDocuments({ user_id: userId, collection_id: "kb-1" })[0]?.status).toBe("ready");
  });

  it("clears partial vectors and marks documents failed when collection reindex aborts mid-batch", async () => {
    let failAfterBatches = Number.POSITIVE_INFINITY;
    let batches = 0;
    const embed: EmbeddingService = {
      embed: async (texts) => {
        batches += 1;
        if (batches > failAfterBatches) {
          throw new Error("EMBEDDING_REQUEST_FAILED:500:mid-reindex");
        }
        return texts.map((_, index) => [index + 1, 0, 0]);
      }
    };
    const { knowledge, metadataStore, userId } = createHarness(embed);
    for (let index = 0; index < 33; index += 1) {
      const doc = await knowledge.ingestText({
        user_id: userId,
        collection_id: "kb-1",
        filename: `doc-${index}.md`,
        content: `alpha revenue metric document ${index} unique content here`
      });
      expect(doc.status).toBe("ready");
    }

    batches = 0;
    failAfterBatches = 1;
    await expect(knowledge.reindex({
      user_id: userId,
      collection_id: "kb-1"
    })).rejects.toThrow(/EMBEDDING_REQUEST_FAILED:500:mid-reindex/);

    const documents = knowledge.listDocuments({ user_id: userId, collection_id: "kb-1" });
    expect(documents).toHaveLength(33);
    expect(documents.every((item) => item.status === "failed")).toBe(true);

    const embeddingCount = metadataStore.db.prepare(
      "SELECT COUNT(*) AS count FROM knowledge_embeddings WHERE user_id = ? AND collection_id = ?"
    ).get(userId, "kb-1") as { count: number };
    expect(embeddingCount.count).toBe(0);
  });

  it("marks failed when single-document reindex fails", async () => {
    const embed: EmbeddingService = {
      embed: async () => {
        throw new Error("EMBEDDING_REQUEST_FAILED:500:still-broken");
      }
    };
    // FTS-only ingest (no embedding key) leaves a ready document with chunks.
    const { knowledge, metadataStore, userId } = createHarness();
    const doc = await knowledge.ingestText({
      user_id: userId,
      collection_id: "kb-1",
      filename: "fts-only.md",
      content: "alpha revenue metric fts"
    });
    expect(doc.status).toBe("ready");

    const knowledgeWithEmbed = new LocalKnowledgeService(metadataStore, {
      embedding: embeddingConfig,
      embeddingService: embed
    });

    await expect(knowledgeWithEmbed.reindexDocument({
      user_id: userId,
      collection_id: "kb-1",
      document_id: doc.id
    })).rejects.toThrow(/EMBEDDING_REQUEST_FAILED/);

    expect(knowledgeWithEmbed.listDocuments({ user_id: userId, collection_id: "kb-1" })[0]?.status)
      .toBe("failed");
  });
});
