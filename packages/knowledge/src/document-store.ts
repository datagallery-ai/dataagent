import type { MetadataStore } from "@datafoundry/metadata";
import { randomUUID } from "node:crypto";
import type {
  DocumentRecord,
  KnowledgeChunkRow,
  KnowledgeRetrievalPolicy,
  RetrievedChunk
} from "./types.js";

export type CreateKnowledgeDocumentInput = {
  user_id: string;
  collection_id: string;
  filename: string;
  chunks: string[];
  file_asset_ref_id?: string;
  mime_type?: string;
};

export interface KnowledgeDocumentStore {
  createDocumentWithChunks(input: CreateKnowledgeDocumentInput): string;
  /** Hard-delete document row and FTS chunks. Callers clear embeddings separately. */
  deleteDocument(input: { document_id: string; user_id: string }): void;
  ensureDocumentFileAssetRefColumn(): void;
  findChunkId(input: { document_id: string; index: number; user_id: string }): string;
  getDocument(input: { document_id: string; user_id: string }): DocumentRecord;
  initializeSchema(): void;
  listChunkRows(input: { collection_id: string; user_id: string }): KnowledgeChunkRow[];
  listChunkRowsForDocument(input: { document_id: string; user_id: string }): KnowledgeChunkRow[];
  listDocuments(input: { collection_id: string; user_id: string }): DocumentRecord[];
  markDocumentFailed(input: { document_id: string; user_id: string }): void;
  markDocumentReady(input: { document_id: string; user_id: string }): void;
  markDocumentsFailed(input: { collection_id: string; user_id: string }): void;
  markDocumentsReady(input: { collection_id: string; user_id: string }): void;
  retrieveFullText(input: {
    collection_id: string;
    policy: KnowledgeRetrievalPolicy;
    query: string;
    user_id: string;
  }): RetrievedChunk[];
}

export class LocalSqliteKnowledgeDocumentStore implements KnowledgeDocumentStore {
  constructor(private readonly metadataStore: MetadataStore) {}

  createDocumentWithChunks(input: CreateKnowledgeDocumentInput): string {
    const documentId = randomUUID();
    const now = new Date().toISOString();
    this.metadataStore.db.prepare(`
      INSERT INTO knowledge_documents (id, user_id, collection_id, filename, mime_type, status, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, 'indexing', ?, ?)
    `).run(
      documentId,
      input.user_id,
      input.collection_id,
      input.filename,
      input.mime_type ?? "text/plain",
      now,
      now
    );
    if (input.file_asset_ref_id) {
      this.metadataStore.db.prepare(`
        UPDATE knowledge_documents SET file_asset_ref_id = ? WHERE user_id = ? AND id = ?
      `).run(input.file_asset_ref_id, input.user_id, documentId);
    }
    input.chunks.forEach((content, index) => {
      this.metadataStore.db.prepare(`
        INSERT INTO knowledge_chunks (
          id, user_id, collection_id, document_id, filename, chunk_index, content
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
      `).run(randomUUID(), input.user_id, input.collection_id, documentId, input.filename, index, content);
    });
    return documentId;
  }

  deleteDocument(input: { document_id: string; user_id: string }): void {
    this.metadataStore.db.prepare(
      "DELETE FROM knowledge_chunks WHERE user_id = ? AND document_id = ?"
    ).run(input.user_id, input.document_id);
    this.metadataStore.db.prepare(
      "DELETE FROM knowledge_documents WHERE user_id = ? AND id = ?"
    ).run(input.user_id, input.document_id);
  }

  ensureDocumentFileAssetRefColumn(): void {
    const hasColumn = this.metadataStore.db.prepare("PRAGMA table_info(knowledge_documents)").all()
      .some((row) => isRecord(row) && row.name === "file_asset_ref_id");
    if (!hasColumn) {
      this.metadataStore.db.exec("ALTER TABLE knowledge_documents ADD COLUMN file_asset_ref_id TEXT");
    }
  }

  findChunkId(input: { document_id: string; index: number; user_id: string }): string {
    const row = this.metadataStore.db.prepare(`
      SELECT id FROM knowledge_chunks WHERE user_id = ? AND document_id = ? AND chunk_index = ?
    `).get(input.user_id, input.document_id, input.index);
    if (!isRecord(row)) {
      throw new Error(`KNOWLEDGE_CHUNK_NOT_FOUND:${input.document_id}:${input.index}`);
    }
    return requiredString(row, "id");
  }

  getDocument(input: { document_id: string; user_id: string }): DocumentRecord {
    const row = this.metadataStore.db.prepare(
      "SELECT * FROM knowledge_documents WHERE user_id = ? AND id = ?"
    ).get(input.user_id, input.document_id);
    if (!isRecord(row)) {
      throw new Error(`KNOWLEDGE_DOCUMENT_NOT_FOUND:${input.document_id}`);
    }
    return mapDocument(row);
  }

  initializeSchema(): void {
    this.metadataStore.db.exec(`
      CREATE TABLE IF NOT EXISTS knowledge_documents (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        collection_id TEXT NOT NULL,
        filename TEXT NOT NULL,
        file_asset_ref_id TEXT,
        mime_type TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
      );
      CREATE INDEX IF NOT EXISTS idx_knowledge_documents_scope
        ON knowledge_documents(user_id, collection_id, created_at DESC);
      CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks USING fts5(
        id UNINDEXED,
        user_id UNINDEXED,
        collection_id UNINDEXED,
        document_id UNINDEXED,
        filename UNINDEXED,
        chunk_index UNINDEXED,
        content,
        tokenize = 'unicode61'
      );
    `);
  }

  listChunkRows(input: { collection_id: string; user_id: string }): KnowledgeChunkRow[] {
    return this.metadataStore.db.prepare(`
      SELECT id, document_id, filename, content FROM knowledge_chunks WHERE user_id = ? AND collection_id = ?
      ORDER BY document_id, chunk_index
    `).all(input.user_id, input.collection_id).filter(isRecord).map((row) => ({
      id: requiredString(row, "id"),
      document_id: requiredString(row, "document_id"),
      filename: requiredString(row, "filename"),
      content: requiredString(row, "content")
    }));
  }

  listChunkRowsForDocument(input: { document_id: string; user_id: string }): KnowledgeChunkRow[] {
    return this.metadataStore.db.prepare(`
      SELECT id, document_id, filename, content FROM knowledge_chunks
      WHERE user_id = ? AND document_id = ?
      ORDER BY chunk_index
    `).all(input.user_id, input.document_id).filter(isRecord).map((row) => ({
      id: requiredString(row, "id"),
      document_id: requiredString(row, "document_id"),
      filename: requiredString(row, "filename"),
      content: requiredString(row, "content")
    }));
  }

  listDocuments(input: { collection_id: string; user_id: string }): DocumentRecord[] {
    return this.metadataStore.db.prepare(`
      SELECT * FROM knowledge_documents WHERE user_id = ? AND collection_id = ? ORDER BY created_at DESC
    `).all(input.user_id, input.collection_id).filter(isRecord).map(mapDocument);
  }

  markDocumentFailed(input: { document_id: string; user_id: string }): void {
    this.metadataStore.db.prepare(`
      UPDATE knowledge_documents SET status = 'failed', updated_at = ? WHERE user_id = ? AND id = ?
    `).run(new Date().toISOString(), input.user_id, input.document_id);
  }

  markDocumentReady(input: { document_id: string; user_id: string }): void {
    this.metadataStore.db.prepare(`
      UPDATE knowledge_documents SET status = 'ready', updated_at = ? WHERE user_id = ? AND id = ?
    `).run(new Date().toISOString(), input.user_id, input.document_id);
  }

  markDocumentsFailed(input: { collection_id: string; user_id: string }): void {
    this.metadataStore.db.prepare(`
      UPDATE knowledge_documents SET status = 'failed', updated_at = ?
      WHERE user_id = ? AND collection_id = ?
    `).run(new Date().toISOString(), input.user_id, input.collection_id);
  }

  markDocumentsReady(input: { collection_id: string; user_id: string }): void {
    this.metadataStore.db.prepare(`
      UPDATE knowledge_documents SET status = 'ready', updated_at = ?
      WHERE user_id = ? AND collection_id = ?
    `).run(new Date().toISOString(), input.user_id, input.collection_id);
  }

  retrieveFullText(input: {
    collection_id: string;
    policy: KnowledgeRetrievalPolicy;
    query: string;
    user_id: string;
  }): RetrievedChunk[] {
    const terms = tokenizeQuery(input.query);
    if (!terms) {
      return [];
    }
    const rows = this.metadataStore.db.prepare(`
      SELECT id, document_id, filename, content, bm25(knowledge_chunks) AS rank
      FROM knowledge_chunks
      WHERE knowledge_chunks MATCH ? AND user_id = ? AND collection_id = ?
      ORDER BY rank ASC
      LIMIT ?
    `).all(terms, input.user_id, input.collection_id, input.policy.topK);
    return rows.filter(isRecord).map((row) => ({
      document_id: requiredString(row, "document_id"),
      chunk_id: requiredString(row, "id"),
      filename: requiredString(row, "filename"),
      quote: requiredString(row, "content").slice(0, 500),
      content: requiredString(row, "content"),
      score: rankToScore(row.rank)
    }));
  }
}

const tokenizeQuery = (query: string): string => query
  .trim()
  .split(/[^\p{L}\p{N}_-]+/gu)
  .filter((value) => value.length > 1)
  .slice(0, 12)
  .map((value) => `"${value.replaceAll('"', '""')}"`)
  .join(" OR ");

const mapDocument = (row: Record<string, unknown>): DocumentRecord => {
  const fileAssetRefId = optionalString(row.file_asset_ref_id);
  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    collection_id: requiredString(row, "collection_id"),
    filename: requiredString(row, "filename"),
    ...(fileAssetRefId ? { file_asset_ref_id: fileAssetRefId } : {}),
    mime_type: requiredString(row, "mime_type"),
    status: requiredString(row, "status") as DocumentRecord["status"]
  };
};

const rankToScore = (value: unknown): number => typeof value === "number" ? 1 / (1 + Math.abs(value)) : 0;

const optionalString = (value: unknown): string | undefined =>
  typeof value === "string" && value.trim() ? value.trim() : undefined;

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

const requiredString = (row: Record<string, unknown>, key: string): string => {
  const value = row[key];
  if (typeof value !== "string") {
    throw new Error(`KNOWLEDGE_COLUMN_INVALID:${key}`);
  }
  return value;
};
