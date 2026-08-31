import { createErrorResult, createSuccessResult } from "@datafoundry/contracts";
import type { ConfigApiResponse } from "./types.js";

const CAPABILITIES = {
  "artifact.export": true,
  "artifact.list": true,
  "artifact.promote": true,
  "chat.fileUpload": true,
  "chat.imageInput": true,
  "conversation.memory": true,
  "conversation.title": true,
  "interaction.resume": true,
  "datasource.fieldMasking": true,
  "datasource.extendedTypes": true,
  "datasource.introspectionPolicy": true,
  "datasource.queryPolicy": true,
  "datasource.samplePolicy": true,
  "datasource.server": true,
  files: true,
  "kb.chunking": true,
  "kb.citationPolicy": true,
  "kb.scope": true,
  "llm.advancedSampling": true,
  "llm.samplingParams": true,
  knowledge: true,
  mcp: true,
  "mcp.stdio": true,
  "mcp.toolPolicy": true,
  "skill.resourceBinding": true,
  skills: true
};

export const handleCapabilitiesRequest = (method: string | undefined): ConfigApiResponse => {
  if (method !== "GET") {
    return {
      body: createErrorResult("BAD_REQUEST", "Method not allowed."),
      status: 405
    };
  }

  return {
    body: createSuccessResult(CAPABILITIES),
    status: 200
  };
};
