import type { EmbeddingConfig } from "./types.js";

export interface EmbeddingService {
  embed(texts: string[], config: EmbeddingConfig): Promise<number[][]>;
}

export class OpenAICompatibleEmbeddingService implements EmbeddingService {
  async embed(texts: string[], config: EmbeddingConfig): Promise<number[][]> {
    if (!config.api_key) {
      throw new Error("EMBEDDING_API_KEY_REQUIRED");
    }
    const response = await fetch(`${config.base_url.replace(/\/$/u, "")}/embeddings`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${config.api_key}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ input: texts, model: config.model })
    });
    if (!response.ok) {
      const detail = (await response.text()).slice(0, 500);
      throw new Error(`EMBEDDING_REQUEST_FAILED:${response.status}:${detail}`);
    }
    const payload: unknown = await response.json();
    if (!isRecord(payload) || !Array.isArray(payload.data)) {
      throw new Error("EMBEDDING_RESPONSE_INVALID");
    }
    return payload.data.filter(isRecord).sort((left, right) => numberValue(left.index) - numberValue(right.index))
      .map((item) => parseVector(item.embedding));
  }
}

export const parseVector = (value: unknown): number[] => {
  const parsed = typeof value === "string" ? JSON.parse(value) as unknown : value;
  if (!Array.isArray(parsed) || parsed.length === 0 || !parsed.every((item) => typeof item === "number")) {
    throw new Error("EMBEDDING_VECTOR_INVALID");
  }
  return parsed;
};

export const cosineSimilarity = (left: number[], right: number[]): number => {
  if (left.length !== right.length) {
    throw new Error("EMBEDDING_DIMENSION_MISMATCH");
  }
  let dot = 0;
  let leftNorm = 0;
  let rightNorm = 0;
  left.forEach((value, index) => {
    const other = right[index] ?? 0;
    dot += value * other;
    leftNorm += value * value;
    rightNorm += other * other;
  });
  return leftNorm > 0 && rightNorm > 0 ? dot / Math.sqrt(leftNorm * rightNorm) : 0;
};

const numberValue = (value: unknown): number => typeof value === "number" && Number.isFinite(value) ? value : 0;

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
