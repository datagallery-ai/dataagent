import type { ProtocolRegistry, RegisteredProtocolDefinition } from "./protocol-registry.js";

export type ProtocolIdentity = {
  protocolId: string;
  protocolVersion: string;
};

export type TaskRelation = "continue" | "refine" | "replace" | "side-chat";

export type ProtocolRouteSource = "explicit" | "deterministic" | "classifier" | "default";

export type ProtocolRouteInput = {
  authorizedProtocolIds: string[];
  classificationInput?: unknown;
  deterministicCandidates?: Array<ProtocolIdentity & {
    priority: number;
    reasonCode: string;
    taskRelation: TaskRelation;
  }>;
  explicit?: ProtocolIdentity & { taskRelation?: TaskRelation };
};

export type ProtocolRouteClassification = ProtocolIdentity & {
  confidence: number;
  reasonCodes: string[];
  taskRelation: TaskRelation;
};

export type ProtocolClassifier = (input: {
  candidates: ProtocolIdentity[];
  value: unknown;
}) => Promise<ProtocolRouteClassification>;

export type ProtocolRouterOptions = {
  classifier?: ProtocolClassifier;
  confidenceThreshold?: number;
  defaultProtocol?: ProtocolIdentity;
};

export type ProtocolRouteResult = {
  definition: RegisteredProtocolDefinition;
  reasonCodes: string[];
  source: ProtocolRouteSource;
  warnings: string[];
  taskRelation: TaskRelation;
};

export class ProtocolRouter {
  constructor(
    private readonly registry: ProtocolRegistry,
    private readonly options: ProtocolRouterOptions = {}
  ) {}

  async route(input: ProtocolRouteInput): Promise<ProtocolRouteResult> {
    let defaultWarning: string | undefined;
    if (input.explicit) {
      if (!input.authorizedProtocolIds.includes(input.explicit.protocolId)) {
        throw new Error(`PROTOCOL_NOT_AUTHORIZED:${input.explicit.protocolId}@${input.explicit.protocolVersion}`);
      }
      const definition = this.registry.find(input.explicit.protocolId, input.explicit.protocolVersion);
      if (!definition) {
        throw new Error(`PROTOCOL_NOT_REGISTERED:${input.explicit.protocolId}@${input.explicit.protocolVersion}`);
      }
      return {
        definition,
        reasonCodes: ["USER_EXPLICIT"],
        source: "explicit",
        warnings: [],
        taskRelation: input.explicit.taskRelation ?? "replace"
      };
    }
    const deterministicCandidates = (input.deterministicCandidates ?? [])
      .filter((candidate) => input.authorizedProtocolIds.includes(candidate.protocolId))
      .map((candidate) => ({
        candidate,
        definition: this.registry.find(candidate.protocolId, candidate.protocolVersion)
      }))
      .filter((entry): entry is typeof entry & { definition: RegisteredProtocolDefinition } => Boolean(entry.definition))
      .sort((left, right) => right.candidate.priority - left.candidate.priority);
    const selected = deterministicCandidates[0];
    const equallyRanked = selected
      ? deterministicCandidates.filter((entry) => entry.candidate.priority === selected.candidate.priority)
      : [];
    if (selected && equallyRanked.length === 1) {
      return {
        definition: selected.definition,
        reasonCodes: [selected.candidate.reasonCode],
        source: "deterministic",
        warnings: [],
        taskRelation: selected.candidate.taskRelation
      };
    }
    if (equallyRanked.length > 1 && !this.options.classifier) {
      const keys = equallyRanked
        .map((entry) => `${entry.definition.id}@${entry.definition.version}`)
        .sort();
      throw new Error(`PROTOCOL_AMBIGUOUS:${keys.join(",")}`);
    }
    if (this.options.classifier) {
      const candidates = this.registry.list()
        .filter((definition) => input.authorizedProtocolIds.includes(definition.id))
        .map((definition) => ({ protocolId: definition.id, protocolVersion: definition.version }));
      let classification: ProtocolRouteClassification | undefined;
      try {
        classification = await this.options.classifier({ candidates, value: input.classificationInput });
      } catch {
        defaultWarning = "PROTOCOL_CLASSIFICATION_FAILED";
      }
      if (classification) {
        const definition = candidates.some((candidate) =>
          candidate.protocolId === classification.protocolId
          && candidate.protocolVersion === classification.protocolVersion)
          ? this.registry.find(classification.protocolId, classification.protocolVersion)
          : undefined;
        if (!definition) {
          throw new Error(
            `PROTOCOL_CLASSIFIER_INVALID_SELECTION:${classification.protocolId}@${classification.protocolVersion}`
          );
        }
        if (classification.confidence >= (this.options.confidenceThreshold ?? 0.75)) {
          return {
            definition,
            reasonCodes: classification.reasonCodes,
            source: "classifier",
            warnings: [],
            taskRelation: classification.taskRelation
          };
        }
        defaultWarning = "PROTOCOL_CLASSIFICATION_LOW_CONFIDENCE";
      }
    }
    const defaultProtocol = this.options.defaultProtocol ?? {
      protocolId: "general-task",
      protocolVersion: "1"
    };
    if (input.authorizedProtocolIds.includes(defaultProtocol.protocolId)) {
      const definition = this.registry.find(defaultProtocol.protocolId, defaultProtocol.protocolVersion);
      if (definition) {
        return {
          definition,
          reasonCodes: ["GENERAL_TASK_DEFAULT"],
          source: "default",
          warnings: defaultWarning ? [defaultWarning] : [],
          taskRelation: "side-chat"
        };
      }
    }
    throw new Error("PROTOCOL_NOT_RESOLVED");
  }
}
