import type { ApiResult } from "@datafoundry/contracts";
import type { LocalDataGateway } from "@datafoundry/data-gateway";
import type { FileAssetService } from "@datafoundry/files";
import type { LocalKnowledgeService } from "@datafoundry/knowledge";
import type { MetadataStore } from "@datafoundry/metadata";
import type { RunCancelRegistry } from "../run-cancel-registry.js";

export type ConfigApiContext = {
  dataGateway: LocalDataGateway;
  fileAssetService: FileAssetService;
  knowledgeService: LocalKnowledgeService;
  metadataStore: MetadataStore;
  runCancelRegistry: RunCancelRegistry;
  userId: string;
  workspaceId?: string;
};

export type ConfigApiResponse = {
  body: ApiResult<unknown> | Buffer | Record<string, unknown>;
  headers?: Record<string, string>;
  status: number;
};
