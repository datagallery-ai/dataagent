import type { MetadataStore } from "@datafoundry/metadata";
import { cosineSimilarity, parseVector } from "./embedding-service.js";
import type { KnowledgeChunkRow, RetrievedChunk } from "./types.js";

export type VectorStoreUpsertRow = KnowledgeChunkRow & {
  vector: number[];
};

export type VectorStoreQueryInput = {
  collection_id: string;
  query_vector: number[];
  top_k: number;
  user_id: string;
};

export interface VectorStore {
  clearCollection(input: { collection_id: string; user_id: string }): void;
  clearDocument(input: { document_id: string; user_id: string }): void;
  hasIndex(input: { collection_id: string; user_id: string }): boolean;
  initializeSchema(): void;
  query(input: VectorStoreQueryInput): RetrievedChunk[];
  upsertRows(input: { collection_id: string; rows: VectorStoreUpsertRow[]; user_id: string }): void;
}

export class LocalSqliteVectorStore implements VectorStore {
  constructor(private readonly metadataStore: MetadataStore) {}

  clearCollection(input: { collection_id: string; user_id: string }): void {
    this.metadataStore.db.prepare(
      "DELETE FROM knowledge_embeddings WHERE user_id = ? AND collection_id = ?"
    ).run(input.user_id, input.collection_id);
  }

  clearDocument(input: { document_id: string; user_id: string }): void {
    this.metadataStore.db.prepare(
      "DELETE FROM knowledge_embeddings WHERE user_id = ? AND document_id = ?"
    ).run(input.user_id, input.document_id);
  }

  hasIndex(input: { collection_id: string; user_id: string }): boolean {
    const row = this.metadataStore.db.prepare(`
      SELECT 1 AS present FROM knowledge_embeddings WHERE user_id = ? AND collection_id = ? LIMIT 1
    `).get(input.user_id, input.collection_id);
    return isRecord(row);
  }

  initializeSchema(): void {
    this.metadataStore.db.exec(`
      CREATE TABLE IF NOT EXISTS knowledge_embeddings (
        chunk_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        collection_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        filename TEXT NOT NULL,
        content TEXT NOT NULL,
        vector_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
      );
      CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_scope
        ON knowledge_embeddings(user_id, collection_id);
    `);
  }

  query(input: VectorStoreQueryInput): RetrievedChunk[] {
    const rows = this.metadataStore.db.prepare(`
      SELECT chunk_id, document_id, filename, content, vector_json FROM knowledge_embeddings
      WHERE user_id = ? AND collection_id = ?
    `).all(input.user_id, input.collection_id).filter(isRecord);

    return rows.map((row) => ({
      document_id: requiredString(row, "document_id"),
      chunk_id: requiredString(row, "chunk_id"),
      filename: requiredString(row, "filename"),
      quote: requiredString(row, "content").slice(0, 500),
      content: requiredString(row, "content"),
      score: cosineSimilarity(input.query_vector, parseVector(row.vector_json))
    })).sort((left, right) => right.score - left.score).slice(0, input.top_k);
  }

  upsertRows(input: { collection_id: string; rows: VectorStoreUpsertRow[]; user_id: string }): void {
    const now = new Date().toISOString();
    input.rows.forEach((row) => {
      this.metadataStore.db.prepare(`
        INSERT INTO knowledge_embeddings (
          chunk_id, user_id, collection_id, document_id, filename, content, vector_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      `).run(
        row.id,
        input.user_id,
        input.collection_id,
        row.document_id,
        row.filename,
        row.content,
        JSON.stringify(row.vector),
        now
      );
    });
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

const requiredString = (row: Record<string, unknown>, key: string): string => {
  const value = row[key];
  if (typeof value !== "string") {
    throw new Error(`KNOWLEDGE_COLUMN_INVALID:${key}`);
  }
  return value;
};
