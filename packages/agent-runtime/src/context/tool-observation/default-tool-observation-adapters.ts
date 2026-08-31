import {
  AskUserToolObservationAdapter,
  SubmitPlanToolObservationAdapter
} from "./adapters/collaboration-tool-observation-adapters.js";
import {
  ListDataSourcesToolObservationAdapter,
  PreviewTableToolObservationAdapter,
  RetrieveKnowledgeToolObservationAdapter
} from "./adapters/data-tool-observation-adapters.js";
import { SchemaToolObservationAdapter } from "./adapters/schema-tool-observation-adapter.js";
import {
  SkillActivationToolObservationAdapter,
  SkillReadToolObservationAdapter,
  SkillSearchToolObservationAdapter
} from "./adapters/skill-tool-observation-adapters.js";
import { SqlResultToolObservationAdapter } from "./adapters/sql-result-tool-observation-adapter.js";
import {
  TaskCheckToolObservationAdapter,
  TaskCompleteToolObservationAdapter,
  TaskUpdateToolObservationAdapter,
  TaskWriteToolObservationAdapter
} from "./adapters/task-tool-observation-adapters.js";
import {
  EditFileToolObservationAdapter,
  ExecuteCommandToolObservationAdapter,
  FileStatToolObservationAdapter,
  GrepToolObservationAdapter,
  ListFilesToolObservationAdapter,
  ListWorkspaceFilesToolObservationAdapter,
  MkdirToolObservationAdapter,
  PromoteWorkspaceFileToolObservationAdapter,
  ReadFileToolObservationAdapter,
  ReadWorkspaceFileToolObservationAdapter,
  WriteFileToolObservationAdapter
} from "./adapters/workspace-tool-observation-adapters.js";
import { McpToolObservationAdapter } from "./adapters/mcp-tool-observation-adapter.js";
import type { ToolObservationAdapter } from "./tool-observation-adapter.js";
import type { ToolObservationAdapterRegistry } from "./tool-observation-adapter-registry.js";

export type RegisterDefaultToolObservationAdaptersInput = {
  additionalAdapters?: ToolObservationAdapter[];
  includeKnowledge?: boolean;
  mcpToolNames?: string[];
  registry: ToolObservationAdapterRegistry;
};

export const registerDefaultToolObservationAdapters = (
  input: RegisterDefaultToolObservationAdaptersInput
): void => {
  defaultToolObservationAdapters(input).forEach((adapter) => input.registry.register(adapter));
};

const defaultToolObservationAdapters = (
  input: RegisterDefaultToolObservationAdaptersInput
): ToolObservationAdapter[] => [
  new SchemaToolObservationAdapter(),
  new SqlResultToolObservationAdapter(),
  new ListDataSourcesToolObservationAdapter(),
  new PreviewTableToolObservationAdapter(),
  ...(input.includeKnowledge ? [new RetrieveKnowledgeToolObservationAdapter()] : []),
  new ReadFileToolObservationAdapter(),
  new WriteFileToolObservationAdapter(),
  new EditFileToolObservationAdapter(),
  new ListFilesToolObservationAdapter(),
  new GrepToolObservationAdapter(),
  new FileStatToolObservationAdapter(),
  new MkdirToolObservationAdapter(),
  new ExecuteCommandToolObservationAdapter(),
  new PromoteWorkspaceFileToolObservationAdapter(),
  new ListWorkspaceFilesToolObservationAdapter(),
  new ReadWorkspaceFileToolObservationAdapter(),
  new SkillActivationToolObservationAdapter(),
  new SkillSearchToolObservationAdapter(),
  new SkillReadToolObservationAdapter(),
  new TaskWriteToolObservationAdapter(),
  new TaskUpdateToolObservationAdapter(),
  new TaskCompleteToolObservationAdapter(),
  new TaskCheckToolObservationAdapter(),
  new AskUserToolObservationAdapter(),
  new SubmitPlanToolObservationAdapter(),
  ...(input.mcpToolNames ?? []).map((name) => new McpToolObservationAdapter(name)),
  ...(input.additionalAdapters ?? [])
];
