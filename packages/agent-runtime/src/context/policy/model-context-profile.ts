export type ModelContextProfile = {
  id: string;
  modelPattern: string;
  contextWindow: number;
  outputReserve: number;
  safetyMargin: number;
  messageOverhead: number;
  toolSchemaOverhead: number;
};

export type ModelContextProfileRegistryOptions = {
  defaultProfile?: ModelContextProfile;
  profiles?: ModelContextProfile[];
};

// Model context window for budget planning. This is the *model's* total token window,
// NOT the per-source shaping budget (CONTEXT_MAX_TOKENS). Conflating the two starves the
// fixed cost (system + tool schemas) once the full toolset is enabled. Default targets the
// 128K window of the bundled `qwen-plus` model; override per deployment when the model differs.
const FALLBACK_MODEL_CONTEXT_WINDOW = 128_000;

const resolveDefaultContextWindow = (): number => {
  const parsed = Number.parseInt(process.env.AGENT_MODEL_CONTEXT_WINDOW ?? "", 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : FALLBACK_MODEL_CONTEXT_WINDOW;
};

const DEFAULT_PROFILE: ModelContextProfile = {
  id: "conservative-default",
  modelPattern: "*",
  contextWindow: resolveDefaultContextWindow(),
  outputReserve: 4096,
  safetyMargin: 2048,
  messageOverhead: 4,
  toolSchemaOverhead: 32
};

export class ModelContextProfileRegistry {
  private readonly defaultProfile: ModelContextProfile;
  private readonly profiles: ModelContextProfile[];

  constructor(options: ModelContextProfileRegistryOptions = {}) {
    this.defaultProfile = options.defaultProfile ?? DEFAULT_PROFILE;
    this.profiles = [...(options.profiles ?? [])];
  }

  resolve(modelName?: string): ModelContextProfile {
    const normalized = modelName?.toLowerCase() ?? "";
    return this.profiles.find(
      (profile) => normalized.includes(profile.modelPattern.toLowerCase())
    ) ?? this.defaultProfile;
  }
}
