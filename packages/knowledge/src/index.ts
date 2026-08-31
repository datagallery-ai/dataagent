import type { MetadataStore } from "@datafoundry/metadata";
import {
  OpenAICompatibleEmbeddingService,
  type EmbeddingService
} from "./embedding-service.js";
import {
  LocalSqliteKnowledgeDocumentStore,
  type KnowledgeDocumentStore
} from "./document-store.js";
import {
  LocalSqliteVectorStore,
  type VectorStore,
  type VectorStoreUpsertRow
} from "./vector-store.js";
import {
  LocalKnowledgeRetriever,
  type KnowledgeRetriever
} from "./retriever.js";
import type {
  DocumentRecord,
  EmbeddingConfig,
  KnowledgeChunkPolicy,
  KnowledgeChunkRow,
  KnowledgeRetrievalPolicy,
  KnowledgeService,
  LocalKnowledgeServiceOptions,
  RetrieveKnowledgeInput,
  RetrievedChunk
} from "./types.js";

export type {
  DocumentRecord,
  EmbeddingConfig,
  KnowledgeChunkPolicy,
  KnowledgeCollection,
  KnowledgeRetrievalPolicy,
  KnowledgeService,
  LocalKnowledgeServiceOptions,
  RetrieveKnowledgeInput,
  RetrievedChunk
} from "./types.js";
export type { EmbeddingService } from "./embedding-service.js";
export type { KnowledgeRetrievePlan, KnowledgeRetriever } from "./retriever.js";
export type { VectorStore } from "./vector-store.js";

export class LocalKnowledgeService implements KnowledgeService {
  private readonly documentStore: KnowledgeDocumentStore;
  private readonly embeddingService: EmbeddingService;
  private readonly retriever: KnowledgeRetriever;
  private readonly vectorStore: VectorStore;

  constructor(
    private readonly metadataStore: MetadataStore,
    private readonly options: LocalKnowledgeServiceOptions = {}
  ) {
    this.documentStore = new LocalSqliteKnowledgeDocumentStore(metadataStore);
    this.embeddingService = options.embeddingService ?? new OpenAICompatibleEmbeddingService();
    this.vectorStore = new LocalSqliteVectorStore(metadataStore);
    this.retriever = new LocalKnowledgeRetriever(this.documentStore, this.vectorStore, this.embeddingService);
    this.initializeSchema();
  }

  /** Store one text document as bounded searchable chunks. */
  async ingestText(input: {
    user_id: string;
    workspace_id?: string;
    collection_id: string;
    filename: string;
    content: string;
    file_asset_ref_id?: string;
    mime_type?: string;
  }): Promise<DocumentRecord> {
    if (!input.content.trim()) {
      throw new Error("KNOWLEDGE_DOCUMENT_EMPTY");
    }
    const chunks = splitText(input.content, this.resolveChunkPolicy(
      input.user_id,
      input.collection_id,
      input.workspace_id ?? "default"
    ));
    const documentId = this.documentStore.createDocumentWithChunks({
      user_id: input.user_id,
      collection_id: input.collection_id,
      filename: input.filename,
      chunks,
      ...(input.file_asset_ref_id ? { file_asset_ref_id: input.file_asset_ref_id } : {}),
      ...(input.mime_type ? { mime_type: input.mime_type } : {})
    });
    const embedding = this.resolveEmbeddingConfig(input.user_id, input.collection_id, input.workspace_id ?? "default");
    try {
      if (embedding?.api_key) {
        await this.indexDocumentChunks({
          user_id: input.user_id,
          collection_id: input.collection_id,
          document_id: documentId,
          filename: input.filename,
          chunks,
          embedding
        });
      }
    } catch (error) {
      this.vectorStore.clearDocument({ user_id: input.user_id, document_id: documentId });
      this.documentStore.markDocumentFailed({ user_id: input.user_id, document_id: documentId });
      throw error;
    }
    this.documentStore.markDocumentReady({ user_id: input.user_id, document_id: documentId });
    return this.documentStore.getDocument({ user_id: input.user_id, document_id: documentId });
  }

  /** Retrieve the most relevant local full-text chunks with citations. */
  async retrieve(input: RetrieveKnowledgeInput): Promise<RetrievedChunk[]> {
    const embedding = this.resolveEmbeddingConfig(input.user_id, input.collection_id, input.workspace_id ?? "default");
    const policy = this.resolveRetrievalPolicy(input);
    return await this.retriever.retrieve({
      ...(embedding ? { embedding } : {}),
      input,
      policy
    });
  }

  /**
   * Hard-delete one document and cascade clear chunks/FTS + embeddings.
   * MVP: hard delete (not soft `deleted` status) so list/search cannot resurface it.
   */
  deleteDocument(input: {
    user_id: string;
    collection_id: string;
    document_id: string;
  }): { deleted: boolean; id: string } {
    const document = this.documentStore.getDocument({
      user_id: input.user_id,
      document_id: input.document_id
    });
    if (document.collection_id !== input.collection_id) {
      throw new Error(`KNOWLEDGE_DOCUMENT_NOT_FOUND:${input.document_id}`);
    }
    this.vectorStore.clearDocument({ user_id: input.user_id, document_id: input.document_id });
    this.documentStore.deleteDocument({ user_id: input.user_id, document_id: input.document_id });
    return { deleted: true, id: input.document_id };
  }

  /**
   * Rebuild vectors for one document. Success → status ready; failure → status failed.
   * Failed docs may still match FTS until deleted/retry succeeds (MVP: keep status quo).
   */
  async reindexDocument(input: {
    user_id: string;
    workspace_id?: string;
    collection_id: string;
    document_id: string;
  }): Promise<DocumentRecord> {
    const document = this.documentStore.getDocument({
      user_id: input.user_id,
      document_id: input.document_id
    });
    if (document.collection_id !== input.collection_id) {
      throw new Error(`KNOWLEDGE_DOCUMENT_NOT_FOUND:${input.document_id}`);
    }
    const embedding = this.resolveEmbeddingConfig(
      input.user_id,
      input.collection_id,
      input.workspace_id ?? "default"
    );
    this.vectorStore.clearDocument({ user_id: input.user_id, document_id: input.document_id });
    try {
      if (embedding?.api_key) {
        const rows = this.documentStore.listChunkRowsForDocument({
          user_id: input.user_id,
          document_id: input.document_id
        });
        await this.indexRows(input.user_id, input.collection_id, rows, embedding);
      }
    } catch (error) {
      this.vectorStore.clearDocument({ user_id: input.user_id, document_id: input.document_id });
      this.documentStore.markDocumentFailed({ user_id: input.user_id, document_id: input.document_id });
      throw error;
    }
    this.documentStore.markDocumentReady({ user_id: input.user_id, document_id: input.document_id });
    return this.documentStore.getDocument({ user_id: input.user_id, document_id: input.document_id });
  }

  /** Rebuild vector entries for every current document in one collection. */
  async reindex(input: {
    user_id: string;
    workspace_id?: string;
    collection_id: string;
  }): Promise<{ chunks: number; mode: string }> {
    const embedding = this.resolveEmbeddingConfig(input.user_id, input.collection_id, input.workspace_id ?? "default");
    this.vectorStore.clearCollection({ user_id: input.user_id, collection_id: input.collection_id });
    if (!embedding?.api_key) {
      // FTS-only mode still clears failed→ready so prior vector failures are not stuck forever.
      this.documentStore.markDocumentsReady({
        user_id: input.user_id,
        collection_id: input.collection_id
      });
      return { chunks: 0, mode: "fts" };
    }
    try {
      const rows = this.documentStore.listChunkRows({
        user_id: input.user_id,
        collection_id: input.collection_id
      });
      await this.indexRows(input.user_id, input.collection_id, rows, embedding);
      this.documentStore.markDocumentsReady({
        user_id: input.user_id,
        collection_id: input.collection_id
      });
      return { chunks: rows.length, mode: "vector" };
    } catch (error) {
      // Align with reindexDocument: drop partial upserts and mark the collection failed so
      // status matches vector state and per-document retry remains available.
      this.vectorStore.clearCollection({
        user_id: input.user_id,
        collection_id: input.collection_id
      });
      this.documentStore.markDocumentsFailed({
        user_id: input.user_id,
        collection_id: input.collection_id
      });
      throw error;
    }
  }

  private resolveEmbeddingConfig(userId: string, collectionId: string, workspaceId: string): EmbeddingConfig | undefined {
    const resource = this.metadataStore.configResources.find({
      id: collectionId,
      workspace_id: workspaceId,
      user_id: userId,
      kind: "knowledge-base"
    });
    const payload = resource?.payload ?? {};
    const secret = resource?.secret_ref
      ? this.metadataStore.secrets.get({ ref: resource.secret_ref, workspace_id: workspaceId, user_id: userId })
      : {};
    const fallback = this.options.embedding;
    const apiKey = optionalString(secret.apiKey) ?? optionalString(secret.api_key) ?? fallback?.api_key;
    const baseUrl = optionalString(payload.embeddingBaseUrl) ?? optionalString(payload.baseUrl) ?? fallback?.base_url;
    const model = optionalString(payload.embeddingModel) ?? fallback?.model;
    const provider = optionalString(payload.embeddingProvider) ?? fallback?.provider;
    if (!baseUrl || !model || !provider) {
      return undefined;
    }
    return { ...(apiKey ? { api_key: apiKey } : {}), base_url: baseUrl, model, provider };
  }

  private resolveRetrievalPolicy(input: RetrieveKnowledgeInput): KnowledgeRetrievalPolicy {
    const resource = this.metadataStore.configResources.find({
      id: input.collection_id,
      workspace_id: input.workspace_id ?? "default",
      user_id: input.user_id,
      kind: "knowledge-base"
    });
    const payload = resource?.payload ?? {};
    const configuredTopK = optionalNumber(payload.retrievalTopK) ?? optionalNumber(payload.retrieval_top_k);
    const scoreThreshold = optionalNumber(payload.scoreThreshold) ?? optionalNumber(payload.score_threshold);
    return {
      ...(scoreThreshold !== undefined ? { scoreThreshold: Math.max(0, Math.min(1, scoreThreshold)) } : {}),
      topK: Math.max(1, Math.min(input.top_k ?? configuredTopK ?? 5, 20))
    };
  }

  private resolveChunkPolicy(userId: string, collectionId: string, workspaceId: string): KnowledgeChunkPolicy {
    const resource = this.metadataStore.configResources.find({
      id: collectionId,
      workspace_id: workspaceId,
      user_id: userId,
      kind: "knowledge-base"
    });
    const payload = resource?.payload ?? {};
    const chunkSize = optionalNumber(payload.chunkSize) ?? optionalNumber(payload.chunk_size);
    const chunkOverlap = optionalNumber(payload.chunkOverlap) ?? optionalNumber(payload.chunk_overlap);
    return {
      chunkOverlap: Math.max(0, Math.min(chunkOverlap ?? 200, 1000)),
      chunkSize: Math.max(200, Math.min(chunkSize ?? 1600, 8000))
    };
  }

  private async indexDocumentChunks(input: {
    user_id: string;
    collection_id: string;
    document_id: string;
    filename: string;
    chunks: string[];
    embedding: EmbeddingConfig;
  }): Promise<void> {
    const rows: KnowledgeChunkRow[] = input.chunks.map((content, index) => ({
      id: this.documentStore.findChunkId({
        user_id: input.user_id,
        document_id: input.document_id,
        index
      }),
      document_id: input.document_id,
      filename: input.filename,
      content
    }));
    await this.indexRows(input.user_id, input.collection_id, rows, input.embedding);
  }

  private async indexRows(
    userId: string,
    collectionId: string,
    rows: Record<string, unknown>[],
    embedding: EmbeddingConfig
  ): Promise<void> {
    for (let offset = 0; offset < rows.length; offset += 32) {
      const batch = rows.slice(offset, offset + 32);
      const vectors = await this.embeddingService.embed(batch.map((row) => requiredString(row, "content")), embedding);
      const vectorRows: VectorStoreUpsertRow[] = batch.map((row, index) => {
        const vector = vectors[index];
        if (!vector) {
          throw new Error("EMBEDDING_RESPONSE_COUNT_MISMATCH");
        }
        return {
          id: requiredString(row, "id"),
          document_id: requiredString(row, "document_id"),
          filename: requiredString(row, "filename"),
          content: requiredString(row, "content"),
          vector
        };
      });
      this.vectorStore.upsertRows({ user_id: userId, collection_id: collectionId, rows: vectorRows });
    }
  }

  private findChunkId(userId: string, documentId: string, index: number): string {
    return this.documentStore.findChunkId({ user_id: userId, document_id: documentId, index });
  }

  /** List documents in one knowledge collection. */
  listDocuments(input: { user_id: string; collection_id: string }): DocumentRecord[] {
    return this.documentStore.listDocuments(input);
  }

  private getDocument(input: { user_id: string; document_id: string }): DocumentRecord {
    return this.documentStore.getDocument(input);
  }

  private initializeSchema(): void {
    this.documentStore.initializeSchema();
    this.documentStore.ensureDocumentFileAssetRefColumn();
    this.vectorStore.initializeSchema();
  }
}

const splitText = (content: string, policy: KnowledgeChunkPolicy): string[] => {
  const chunkSize = Math.max(200, Math.floor(policy.chunkSize));
  const overlap = Math.min(Math.max(0, Math.floor(policy.chunkOverlap)), Math.max(0, chunkSize - 1));
  const paragraphs = content.split(/\n\s*\n/gu).map((value) => value.trim()).filter(Boolean);
  const chunks: string[] = [];
  let current = "";
  paragraphs.forEach((paragraph) => {
    if (current && current.length + paragraph.length + 2 > chunkSize) {
      chunks.push(current);
      current = "";
    }
    current = current ? `${current}\n\n${paragraph}` : paragraph;
  });
  if (current) {
    chunks.push(current);
  }
  return chunks.flatMap((chunk) => splitLongChunk(chunk, chunkSize, overlap));
};

const splitLongChunk = (chunk: string, chunkSize: number, overlap: number): string[] => {
  if (chunk.length <= chunkSize) {
    return [chunk];
  }
  const result: string[] = [];
  const step = Math.max(1, chunkSize - overlap);
  for (let start = 0; start < chunk.length; start += step) {
    result.push(chunk.slice(start, start + chunkSize));
  }
  return result;
};

const optionalNumber = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;
const optionalString = (value: unknown): string | undefined =>
  typeof value === "string" && value.trim() ? value.trim() : undefined;
const requiredString = (row: Record<string, unknown>, key: string): string => {
  const value = row[key];
  if (typeof value !== "string") {
    throw new Error(`KNOWLEDGE_COLUMN_INVALID:${key}`);
  }
  return value;
};
