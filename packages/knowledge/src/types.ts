import type { Citation } from "@datafoundry/contracts";
import type { EmbeddingService } from "./embedding-service.js";

export type KnowledgeCollection = {
  id: string;
  user_id: string;
  name: string;
  embedding_provider: string;
  embedding_model: string;
  embedding_dim: number;
  status: "ready" | "indexing" | "failed";
};

export type DocumentRecord = {
  id: string;
  user_id: string;
  collection_id: string;
  filename: string;
  file_asset_ref_id?: string;
  mime_type: string;
  status: "uploaded" | "parsing" | "indexing" | "ready" | "failed" | "deleted";
};

export type RetrieveKnowledgeInput = {
  user_id: string;
  workspace_id?: string;
  collection_id: string;
  query: string;
  top_k?: number;
};

export type KnowledgeRetrievalPolicy = {
  scoreThreshold?: number;
  topK: number;
};

export type KnowledgeChunkPolicy = {
  chunkOverlap: number;
  chunkSize: number;
};

export type RetrievedChunk = Citation & {
  content: string;
};

export type EmbeddingConfig = {
  api_key?: string;
  base_url: string;
  model: string;
  provider: string;
};

export type LocalKnowledgeServiceOptions = {
  embedding?: EmbeddingConfig;
  /** Optional override used by tests and custom runtimes. */
  embeddingService?: EmbeddingService;
};

export interface KnowledgeService {
  retrieve(input: RetrieveKnowledgeInput): Promise<RetrievedChunk[]>;
}

export type KnowledgeChunkRow = {
  content: string;
  document_id: string;
  filename: string;
  id: string;
};
