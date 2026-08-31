// ContextTokenCounter - downloads and caches Hugging Face tokenizers per model
// Falls back to character-length estimation if tokenizer cannot be obtained

import { Tokenizer } from "@huggingface/tokenizers";
import { existsSync, mkdirSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";

export type ContextTokenCounterOptions = {
  cache_dir?: string;
};

// Maps model name patterns to HuggingFace model IDs.
// Keys are matched via substring; order matters — more specific patterns should
// come before broader ones so "qwen3-max" hits before "qwen".
// Tokenizer sizes (approximate, cached locally):
//   Qwen2.5: 6.7MB, Qwen3-235B: 10.9MB, Qwen3-Coder: 6.7MB, QwQ: 6.7MB
//   DeepSeek-V2: 4.4MB, DeepSeek-V3/R1: 7.5MB, DeepSeek-V4: 6.1MB
//   Llama3: 8.7MB, Llama4: 26.7MB, Gemma2: 16.7MB, Gemma3: 31.8MB
const MODEL_TO_HF_ID: Record<string, string> = {
  // Qwen / Alibaba
  // Qwen3-Max / Qwen3.7-Max — different tokenizer from Qwen2.5 (+62% vocab)
  "qwen3.7-max": "Qwen/Qwen3-235B-A22B",
  "qwen3-max": "Qwen/Qwen3-235B-A22B",
  "qwen3.5": "Qwen/Qwen3-235B-A22B",
  "qwen-max": "Qwen/Qwen3-235B-A22B",
  // Qwen3-Coder — uses Qwen2.5-style tokenizer (6.7MB)
  "qwen3-coder": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
  // Qwen2.5 Coder
  "qwen2.5-coder": "Qwen/Qwen2.5-Coder-7B",
  "qwen-coder": "Qwen/Qwen2.5-Coder-7B",
  // Qwen2.5-VL
  "qwen-vl": "Qwen/Qwen2.5-VL-7B-Instruct",
  // QwQ reasoning series
  "qwq-max": "Qwen/QwQ-32B",
  "qwq-plus": "Qwen/QwQ-32B",
  "qwq": "Qwen/QwQ-32B",
  // Qwen2.5 default (catch-all for qwen-plus, qwen-turbo, qwen etc.)
  "qwen-plus": "Qwen/Qwen2.5-7B-Instruct",
  "qwen-turbo": "Qwen/Qwen2.5-7B-Instruct",
  "qwen": "Qwen/Qwen2.5-7B-Instruct",

  // DeepSeek
  // V4 family — new tokenizer class (PreTrainedTokenizerFast, 6.1MB, 1M ctx)
  "deepseek-v4-pro": "deepseek-ai/DeepSeek-V4-Pro",
  "deepseek-v4-flash": "deepseek-ai/DeepSeek-V4-Flash",
  "deepseek-v4": "deepseek-ai/DeepSeek-V4-Flash",
  // V3 / R1 family — LlamaTokenizerFast, 7.5MB
  "deepseek-reasoner": "deepseek-ai/DeepSeek-V3-0324",
  "deepseek-r1": "deepseek-ai/DeepSeek-V3-0324",
  "deepseek-v3": "deepseek-ai/DeepSeek-V3-0324",
  // DeepSeek V2.5
  "deepseek-v2.5": "deepseek-ai/DeepSeek-V2.5",
  // DeepSeek Coder V2
  "deepseek-coder": "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct",
  // DeepSeek Prover (uses V3 tokenizer)
  "deepseek-prover": "deepseek-ai/DeepSeek-Prover-V2-671B",
  // DeepSeek VL
  "deepseek-vl": "deepseek-ai/DeepSeek-VL2",
  // V2 default (catch-all: deepseek, deepseek-chat)
  "deepseek-chat": "deepseek-ai/DeepSeek-V2-Lite",
  "deepseek": "deepseek-ai/DeepSeek-V2-Lite",

  // Meta Llama
  "llama-4": "LLM-Research/Llama-4-Scout-17B-16E-Instruct",
  "llama4": "LLM-Research/Llama-4-Scout-17B-16E-Instruct",
  "llama-3.2": "LLM-Research/Meta-Llama-3-8B-Instruct",
  "llama-3.1": "LLM-Research/Meta-Llama-3-8B-Instruct",
  "llama-3": "LLM-Research/Meta-Llama-3-8B-Instruct",
  "llama3": "LLM-Research/Meta-Llama-3-8B-Instruct",
  "llama": "LLM-Research/Meta-Llama-3-8B-Instruct",

  // Mistral
  "mistral-large": "AI-ModelScope/Mistral-Large-Instruct-2411",
  "mistral-small": "LLM-Research/Mistral-7B-Instruct-v0.3",
  "mistral-tiny": "LLM-Research/Mistral-7B-Instruct-v0.3",
  "mistral": "LLM-Research/Mistral-7B-Instruct-v0.3",
  "mixtral-8x7b": "AI-ModelScope/Mixtral-8x7B-Instruct-v0.1",
  "mixtral": "AI-ModelScope/Mixtral-8x7B-Instruct-v0.1",
  "codestral": "mistralai/Codestral-22B-v0.1",

  // Google Gemma
  "gemma-3": "LLM-Research/gemma-3-12b-it",
  "gemma3": "LLM-Research/gemma-3-12b-it",
  "gemma-2": "AI-ModelScope/gemma-2-9b-it",
  "gemma2": "AI-ModelScope/gemma-2-9b-it",
  "gemma": "AI-ModelScope/gemma-2-9b-it",

  // Microsoft Phi
  "phi-4": "LLM-Research/Phi-4-mini-instruct",
  "phi4": "LLM-Research/Phi-4-mini-instruct",
  "phi-3.5": "LLM-Research/Phi-3.5-mini-instruct",
  "phi-3": "LLM-Research/Phi-3.5-mini-instruct",
  "phi3": "LLM-Research/Phi-3.5-mini-instruct",
  "phi": "LLM-Research/Phi-3.5-mini-instruct",

  // GLM / Zhipu
  // GLM-4 (GLM-5 tokenizers not yet on ModelScope)
  "glm-4": "ZhipuAI/glm-4-9b-chat",
  "glm4": "ZhipuAI/glm-4-9b-chat",
  "glm": "ZhipuAI/glm-4-9b-chat",
  "chatglm3": "ZhipuAI/chatglm3-6b",
  "chatglm": "ZhipuAI/chatglm3-6b",

  // Cohere
  "command-a": "AI-ModelScope/c4ai-command-a-03-2025",
  "command-r-plus": "AI-ModelScope/c4ai-command-r-plus",
  "command-r": "AI-ModelScope/c4ai-command-r-plus",

  // Yi / 01.AI
  "yi-lightning": "01ai/Yi-1.5-9B-Chat",
  "yi-large": "01ai/Yi-1.5-9B-Chat",
  "yi": "01ai/Yi-1.5-9B-Chat",

  // MiniMax
  "minimax-m1": "MiniMax/MiniMax-M1",
  "minimax-text": "MiniMax/MiniMax-Text-01",
  "minimax": "MiniMax/MiniMax-Text-01",

  // InternLM
  "internlm3": "Shanghai_AI_Laboratory/internlm3-8b-instruct",
  "internlm2": "Shanghai_AI_Laboratory/internlm2-chat-7b",
  "internlm": "Shanghai_AI_Laboratory/internlm2-chat-7b",

  // Baichuan
  "baichuan2": "baichuan-inc/Baichuan2-13B-Chat",
  "baichuan": "baichuan-inc/Baichuan2-13B-Chat",

  // Others
  "jamba": "AI-ModelScope/AI21-Jamba-1.5-Mini",
  "minicpm": "OpenBMB/MiniCPM3-4B",
  "telechat": "TeleAI/TeleChat2-115B",
  "bge": "BAAI/bge-large-zh-v1.5",
  "gpt2": "openai-community/gpt2",

  // DeepSeek R1 Distill
  // (inherit tokenizer from their base models)
  "deepseek-r1-distill-qwen": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
  "deepseek-r1-distill-llama": "deepseek-ai/DeepSeek-R1-Distill-Llama-8B",
};

export class ContextTokenCounter {
  private tokenizers = new Map<string, Tokenizer>();
  private loadingPromises = new Map<string, Promise<Tokenizer | null>>();
  private unavailableKeys = new Set<string>();
  private cacheDir: string;

  constructor(options: ContextTokenCounterOptions = {}) {
    this.cacheDir = options.cache_dir ?? join(process.cwd(), ".cache", "tokenizers");
    if (!existsSync(this.cacheDir)) {
      mkdirSync(this.cacheDir, { recursive: true });
    }
  }

  async countTokens(text: string, modelName?: string): Promise<number> {
    const tokenizer = await this.getTokenizer(modelName);

    if (!tokenizer) {
      return this.estimateTokens(text);
    }

    try {
      return tokenizer.encode(text).ids.length;
    } catch (error) {
      console.warn(`ContextTokenCounter: encode failed for ${modelName}, falling back to estimation`, error);
      return this.estimateTokens(text);
    }
  }

  // Sync version for callers that can't await - uses cached tokenizer or estimation
  countTokensSync(text: string, modelName?: string): number {
    const key = this.resolveModelKey(modelName);
    const tokenizer = this.tokenizers.get(key);

    if (!tokenizer) {
      return this.estimateTokens(text);
    }

    try {
      return tokenizer.encode(text).ids.length;
    } catch {
      return this.estimateTokens(text);
    }
  }

  private async getTokenizer(modelName?: string): Promise<Tokenizer | null> {
    const key = this.resolveModelKey(modelName);

    if (this.unavailableKeys.has(key)) {
      return null;
    }

    // Return cached instance
    if (this.tokenizers.has(key)) {
      return this.tokenizers.get(key)!;
    }

    // Deduplicate concurrent loads
    if (this.loadingPromises.has(key)) {
      return this.loadingPromises.get(key)!;
    }

    const hfId = MODEL_TO_HF_ID[key];
    if (!hfId) {
      console.warn(`ContextTokenCounter: no HuggingFace mapping for model "${modelName}", using estimation`);
      return null;
    }

    const loadPromise = this.loadTokenizer(hfId, key);
    this.loadingPromises.set(key, loadPromise);

    try {
      const tokenizer = await loadPromise;
      if (tokenizer) {
        this.tokenizers.set(key, tokenizer);
      } else {
        this.unavailableKeys.add(key);
      }
      return tokenizer;
    } finally {
      this.loadingPromises.delete(key);
    }
  }

  private async loadTokenizer(hfId: string, cacheKey: string): Promise<Tokenizer | null> {
    try {
      const modelCacheDir = join(this.cacheDir, cacheKey.replaceAll(/[^a-zA-Z0-9._-]/g, "_"));
      const tokenizerPath = join(modelCacheDir, "tokenizer.json");
      const configPath = join(modelCacheDir, "tokenizer_config.json");
      mkdirSync(modelCacheDir, { recursive: true });
      const [tokenizerJson, tokenizerConfig] = await Promise.all([
        readOrDownloadJson(tokenizerPath, `${hfId}/resolve/main/tokenizer.json`),
        readOrDownloadJson(configPath, `${hfId}/resolve/main/tokenizer_config.json`)
      ]);
      return new Tokenizer(tokenizerJson, tokenizerConfig);
    } catch (error) {
      console.warn(`ContextTokenCounter: failed to load tokenizer for ${hfId}, using estimation`, error);
      return null;
    }
  }

  private resolveModelKey(modelName?: string): string {
    if (!modelName) {
      return "qwen";
    }

    const lower = modelName.toLowerCase();

    // Check exact matches first
    if (MODEL_TO_HF_ID[lower]) {
      return lower;
    }

    // Check prefix matches
    for (const pattern of Object.keys(MODEL_TO_HF_ID)) {
      if (lower.includes(pattern)) {
        return pattern;
      }
    }

    // Unknown model - return as-is (will have no HF mapping)
    return lower;
  }

  private estimateTokens(text: string): number {
    // Estimation: 1 token ≈ 2 chars for Chinese/CJK, 1 token ≈ 4 chars for English
    let cjkChars = 0;
    let otherChars = 0;

    for (const char of text) {
      const code = char.charCodeAt(0);
      // CJK Unified Ideographs + extensions + fullwidth forms
      if (
        (code >= 0x4E00 && code <= 0x9FFF) ||
        (code >= 0x3400 && code <= 0x4DBF) ||
        (code >= 0xF900 && code <= 0xFAFF) ||
        (code >= 0xFF00 && code <= 0xFFEF)
      ) {
        cjkChars++;
      } else {
        otherChars++;
      }
    }

    return Math.ceil(cjkChars + otherChars / 3);
  }
}

const readOrDownloadJson = async (path: string, modelPath: string): Promise<object> => {
  if (existsSync(path)) {
    return JSON.parse(await readFile(path, "utf8")) as object;
  }

  const response = await fetch(`https://huggingface.co/${modelPath}`);
  if (!response.ok) {
    throw new Error(`TOKENIZER_DOWNLOAD_FAILED:${response.status}`);
  }
  const value = await response.json() as object;
  await writeFile(path, JSON.stringify(value), "utf8");
  return value;
};
