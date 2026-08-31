import type { EmbeddingService } from "./embedding-service.js";
import type { KnowledgeDocumentStore } from "./document-store.js";
import type {
  EmbeddingConfig,
  KnowledgeRetrievalPolicy,
  RetrieveKnowledgeInput,
  RetrievedChunk
} from "./types.js";
import type { VectorStore } from "./vector-store.js";

export type KnowledgeRetrievePlan = {
  embedding?: EmbeddingConfig;
  input: RetrieveKnowledgeInput;
  policy: KnowledgeRetrievalPolicy;
};

export interface KnowledgeRetriever {
  retrieve(plan: KnowledgeRetrievePlan): Promise<RetrievedChunk[]>;
}

export class LocalKnowledgeRetriever implements KnowledgeRetriever {
  constructor(
    private readonly documentStore: KnowledgeDocumentStore,
    private readonly vectorStore: VectorStore,
    private readonly embeddingService: EmbeddingService
  ) {}

  async retrieve(plan: KnowledgeRetrievePlan): Promise<RetrievedChunk[]> {
    const chunks = plan.embedding?.api_key && this.vectorStore.hasIndex({
      user_id: plan.input.user_id,
      collection_id: plan.input.collection_id
    })
      ? await this.retrieveByVector(plan)
      : this.documentStore.retrieveFullText({
          user_id: plan.input.user_id,
          collection_id: plan.input.collection_id,
          query: plan.input.query,
          policy: plan.policy
        });
    const scoreThreshold = plan.policy.scoreThreshold;
    return scoreThreshold === undefined
      ? chunks
      : chunks.filter((chunk) => typeof chunk.score === "number" && chunk.score >= scoreThreshold);
  }

  private async retrieveByVector(plan: KnowledgeRetrievePlan): Promise<RetrievedChunk[]> {
    if (!plan.embedding) {
      return [];
    }
    const [queryVector] = await this.embeddingService.embed([plan.input.query], plan.embedding);
    if (!queryVector) {
      return [];
    }
    return this.vectorStore.query({
      user_id: plan.input.user_id,
      collection_id: plan.input.collection_id,
      query_vector: queryVector,
      top_k: plan.policy.topK
    });
  }
}
