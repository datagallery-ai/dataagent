import { EventType, type BaseEvent } from "@ag-ui/core";
import type { ArtifactSummary, ArtifactType, RunEventEnvelope } from "@datafoundry/contracts";
import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";

import {
  ConfigJobRepository,
  ConfigResourceRepository,
  EncryptedSecretStore,
  initializeConfigSchema
} from "./config-store.js";

export * from "./config-store.js";

export type UserRecord = {
  id: string;
  email?: string;
  display_name?: string;
  dev_token?: string;
  email_verified_at?: string;
  disabled_at?: string;
  password_updated_at?: string;
  created_at: string;
  updated_at: string;
};

export type UserContext = {
  user_id: string;
  email?: string;
  display_name?: string;
};

export type UserPasswordCredentialRecord = {
  user_id: string;
  password_hash: string;
  password_hash_params: string;
  created_at: string;
  updated_at: string;
};

export type AuthSessionRecord = {
  id: string;
  user_id: string;
  token_hash: string;
  csrf_token_hash: string;
  user_agent?: string;
  ip_address?: string;
  expires_at: string;
  revoked_at?: string;
  created_at: string;
  last_seen_at: string;
};

export type AuthTokenPurpose = "email_verification" | "password_reset";

export type AuthTokenRecord = {
  id: string;
  user_id: string;
  purpose: AuthTokenPurpose;
  token_hash: string;
  expires_at: string;
  consumed_at?: string;
  created_at: string;
};

export type WorkspaceRecord = {
  id: string;
  name: string;
  kind: "personal";
  owner_user_id: string;
  created_at: string;
  updated_at: string;
};

export type WorkspaceMembershipRecord = {
  workspace_id: string;
  user_id: string;
  role: "owner";
  created_at: string;
};

export type AuthAuditEventRecord = {
  id: string;
  user_id?: string;
  email?: string;
  event_type: string;
  ip_address?: string;
  user_agent?: string;
  metadata_json?: string;
  created_at: string;
};

export type SessionRecord = {
  id: string;
  user_id: string;
  title?: string;
  title_source?: "llm" | "fallback" | "user";
  last_message_at?: string;
  selected_datasource_id?: string;
  selected_collection_id?: string;
  created_at: string;
  updated_at: string;
};

export type SessionBranchRecord = {
  id: string;
  user_id: string;
  child_session_id: string;
  parent_session_id: string;
  root_session_id: string;
  fork_run_id: string;
  fork_checkpoint_id?: string;
  fork_message_end_position: number;
  created_at: string;
};

export type RunRecord = {
  id: string;
  user_id: string;
  session_id: string;
  parent_run_id?: string;
  request_fingerprint?: string;
  status: "queued" | "running" | "suspended" | "completed" | "failed" | "canceled";
  user_input: string;
  model_provider?: string;
  model_name?: string;
  datasource_id?: string;
  collection_id?: string;
  started_at: string;
  finished_at?: string;
  error_message?: string;
};

export type QueryHistoryRecord = {
  id: string;
  user_id: string;
  workspace_id: string;
  session_id: string;
  run_id?: string;
  datasource_id: string;
  sql_text: string;
  row_count: number;
  elapsed_ms: number;
  favorite: boolean;
  created_at: string;
  updated_at: string;
};

export type InteractionRecord = {
  id: string;
  user_id: string;
  session_id: string;
  run_id: string;
  tool_call_id: string;
  tool_name: "ask_user" | "submit_plan";
  payload_json: string;
  /** Full mastra_suspend interrupt payload for HITL resume after refresh. */
  interrupt_event_json?: string;
  status: "pending" | "resolved" | "canceled";
  resume_fingerprint?: string;
  response_json?: string;
  created_at: string;
  resolved_at?: string;
};

export type RunEventRecord = {
  id: string;
  user_id: string;
  run_id: string;
  session_id: string;
  seq: number;
  event_type: EventType;
  payload_json: string;
  created_at: string;
};

export type ConversationMessageRole = "user" | "assistant";

export type ConversationMessageSource = "client" | "agent";

export type ConversationMessageRecord = {
  id: string;
  user_id: string;
  session_id: string;
  run_id: string;
  role: ConversationMessageRole;
  source: ConversationMessageSource;
  message_id?: string;
  content_json: string;
  content_text: string;
  content_hash: string;
  position: number;
  created_at: string;
};

export type ConversationSummaryRecord = {
  id: string;
  user_id: string;
  session_id: string;
  source_run_id?: string;
  from_position: number;
  to_position: number;
  summary_text: string;
  summary_hash: string;
  created_at: string;
};

export type LongTermMemoryScope = "user" | "session" | "datasource";

export type LongTermMemoryStatus = "active" | "archived";

export type LongTermMemoryRecord = {
  id: string;
  user_id: string;
  scope: LongTermMemoryScope;
  kind: string;
  content_text: string;
  content_json: string;
  memory_hash: string;
  confidence: number;
  status: LongTermMemoryStatus;
  session_id?: string;
  datasource_id?: string;
  source?: string;
  source_run_id?: string;
  created_at: string;
  updated_at: string;
  last_accessed_at?: string;
};

export type ContextPackageSnapshotRecord = {
  id: string;
  user_id: string;
  session_id: string;
  run_id: string;
  package_id: string;
  revision: number;
  payload_json: string;
  plan_json?: string;
  created_at: string;
};

export type ProtocolStateSnapshotRecord = {
  user_id: string;
  run_id: string;
  segment_id: string;
  protocol_id: string;
  protocol_version: string;
  revision: number;
  phase: string;
  status: string;
  context_package_id: string;
  context_package_revision: number;
  state_json: string;
  created_at: string;
  updated_at: string;
};

export type CheckpointKind = "context-compiled" | "protocol-phase" | "run-terminal" | "tool-result";

export type CheckpointStatus = "stable" | "failed" | "terminal";

export type TraceSectionStatus = "completed" | "failed" | "in-progress";

export type TraceSectionRecord = {
  id: string;
  user_id: string;
  session_id: string;
  run_id: string;
  branch_id: string;
  phase_key: string;
  start_event_seq: number;
  end_event_seq: number;
  status: TraceSectionStatus;
  title: string;
  summary: string;
  created_at: string;
  updated_at: string;
};

export type CheckpointRecord = {
  id: string;
  user_id: string;
  session_id: string;
  run_id: string;
  branch_id: string;
  event_seq: number;
  context_package_id: string;
  context_package_revision: number;
  kind: CheckpointKind;
  status: CheckpointStatus;
  label: string;
  context_plan_id?: string;
  parent_checkpoint_id?: string;
  step_number?: number;
  step_id?: string;
  tool_call_id?: string;
  message_position?: number;
  created_at: string;
};

export type ArtifactRecord = {
  id: string;
  user_id: string;
  session_id: string;
  run_id: string;
  type: ArtifactType;
  name: string;
  mime_type?: string;
  storage_path?: string;
  file_asset_ref_id?: string;
  preview_json?: string;
  metadata_json?: string;
  created_at: string;
};

export type ArtifactVersionRecord = {
  id: string;
  user_id: string;
  artifact_id: string;
  version: number;
  file_asset_ref_id?: string;
  preview_json?: string;
  content_hash?: string;
  created_at: string;
};

export type FileAssetRecord = {
  id: string;
  sha256: string;
  size_bytes: number;
  storage_path: string;
  detected_mime_type?: string;
  created_at: string;
};

export type FileAssetRefSource =
  | "artifact"
  | "knowledge"
  | "run-attachment"
  | "skill-package"
  | "upload"
  | "workspace";

export type FileAssetRefStatus = "ready" | "deleted";

export type FileAssetRefRecord = {
  id: string;
  file_asset_id: string;
  user_id: string;
  workspace_id: string;
  filename: string;
  declared_mime_type?: string;
  source: FileAssetRefSource;
  status: FileAssetRefStatus;
  session_id?: string;
  run_id?: string;
  metadata_json?: string;
  created_at: string;
};

export type DataSourceRecord = {
  id: string;
  user_id: string;
  name: string;
  type: string;
  config_json: string;
  credential_ref?: string;
  description?: string;
  status: "ready" | "disabled" | "failed" | "deleted";
  last_test_at?: string;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type SqlAuditLogRecord = {
  id: string;
  user_id: string;
  run_id?: string;
  datasource_id: string;
  sql_text: string;
  status: "succeeded" | "blocked" | "failed" | "timeout" | "canceled";
  blocked_reason?: string;
  row_count?: number;
  elapsed_ms?: number;
  created_at: string;
};

export type MetadataStoreOptions = {
  database_path?: string;
  secret_master_key?: string;
  dev_user?: {
    id: string;
    email: string;
    display_name: string;
    dev_token: string;
  };
};

export type CreateSessionInput = {
  user_id: string;
  id: string;
  title?: string;
  title_source?: "llm" | "fallback" | "user";
  selected_datasource_id?: string;
  selected_collection_id?: string;
};

export type CreateSessionBranchInput = {
  user_id: string;
  id: string;
  child_session_id: string;
  parent_session_id: string;
  root_session_id: string;
  fork_run_id: string;
  fork_checkpoint_id?: string;
  fork_message_end_position: number;
};

export type CreateRunInput = {
  user_id: string;
  id: string;
  session_id: string;
  parent_run_id?: string;
  request_fingerprint?: string;
  user_input: string;
  status?: RunRecord["status"];
  model_provider?: string;
  model_name?: string;
  datasource_id?: string;
  collection_id?: string;
};

export type ClaimRunResult = {
  created: boolean;
  run: RunRecord;
};

export type WriteRunEventInput = {
  user_id: string;
  run_id: string;
  session_id: string;
  event: BaseEvent;
};

export type CreateConversationMessageInput = {
  user_id: string;
  session_id: string;
  run_id: string;
  id: string;
  role: ConversationMessageRole;
  source: ConversationMessageSource;
  content_text: string;
  content?: unknown;
  message_id?: string;
};

export type CreateConversationSummaryInput = {
  user_id: string;
  session_id: string;
  id: string;
  from_position: number;
  to_position: number;
  summary_text: string;
  source_run_id?: string;
};

export type CreateLongTermMemoryInput = {
  id: string;
  user_id: string;
  scope: LongTermMemoryScope;
  kind: string;
  content_text: string;
  content?: unknown;
  confidence?: number;
  session_id?: string;
  datasource_id?: string;
  source?: string;
  source_run_id?: string;
};

export type CreateContextPackageSnapshotInput = {
  user_id: string;
  session_id: string;
  run_id: string;
  package_id: string;
  revision: number;
  payload: unknown;
  plan?: unknown;
};

export type CompareAndSetProtocolStateInput = {
  user_id: string;
  run_id: string;
  segment_id: string;
  expected_revision: number;
  state: unknown;
};

export type TransitionProtocolSegmentInput = {
  user_id: string;
  current: Omit<CompareAndSetProtocolStateInput, "user_id">;
  next: Omit<CompareAndSetProtocolStateInput, "user_id">;
  events?: unknown[];
};

export type CreateCheckpointInput = {
  id: string;
  user_id: string;
  session_id: string;
  run_id: string;
  branch_id?: string;
  event_seq: number;
  context_package_id: string;
  context_package_revision: number;
  kind: CheckpointKind;
  status: CheckpointStatus;
  label: string;
  context_plan_id?: string;
  parent_checkpoint_id?: string;
  step_number?: number;
  step_id?: string;
  tool_call_id?: string;
  message_position?: number;
};

export type UpsertTraceSectionInput = {
  id: string;
  user_id: string;
  session_id: string;
  run_id: string;
  branch_id?: string;
  phase_key: string;
  start_event_seq: number;
  end_event_seq: number;
  status: TraceSectionStatus;
  title: string;
  summary: string;
};

export type ListRelevantLongTermMemoriesInput = {
  user_id: string;
  query: string;
  limit: number;
  session_id?: string;
  datasource_id?: string;
};

export type CreateArtifactInput = {
  user_id: string;
  session_id: string;
  run_id: string;
  id: string;
  type: ArtifactType;
  name: string;
  mime_type?: string;
  storage_path?: string;
  file_asset_ref_id?: string;
  preview_json?: unknown;
  metadata_json?: unknown;
};

export type CreateArtifactVersionInput = {
  id: string;
  user_id: string;
  artifact_id: string;
  file_asset_ref_id?: string;
  preview_json?: unknown;
  content_hash?: string;
};

export type CreateFileAssetInput = {
  id: string;
  sha256: string;
  size_bytes: number;
  storage_path: string;
  detected_mime_type?: string;
};

export type CreateFileAssetRefInput = {
  id: string;
  file_asset_id: string;
  user_id: string;
  workspace_id: string;
  filename: string;
  declared_mime_type?: string;
  source: FileAssetRefSource;
  session_id?: string;
  run_id?: string;
  metadata_json?: unknown;
};

export type CreateDataSourceInput = {
  user_id: string;
  id: string;
  name: string;
  type: string;
  config: Record<string, unknown>;
  credential_ref?: string;
  description?: string;
  status?: DataSourceRecord["status"];
  expected_revision?: number;
};

export type CreateSqlAuditLogInput = {
  user_id: string;
  id: string;
  datasource_id: string;
  sql_text: string;
  status: SqlAuditLogRecord["status"];
  run_id?: string;
  blocked_reason?: string;
  row_count?: number;
  elapsed_ms?: number;
};

export type CreateQueryHistoryInput = {
  user_id: string;
  workspace_id: string;
  session_id: string;
  datasource_id: string;
  sql_text: string;
  row_count: number;
  elapsed_ms: number;
  run_id?: string;
};

const DEFAULT_DEV_USER = {
  id: "dev-user",
  email: "dev@example.com",
  display_name: "Dev User",
  dev_token: "dev-token"
};

export class MetadataStore {
  readonly authAuditEvents: AuthAuditEventRepository;
  readonly authSessions: AuthSessionRepository;
  readonly authTokens: AuthTokenRepository;
  readonly artifactVersions: ArtifactVersionRepository;
  readonly artifacts: ArtifactRepository;
  readonly configJobs: ConfigJobRepository;
  readonly configResources: ConfigResourceRepository;
  readonly checkpoints: CheckpointRepository;
  readonly conversationMessages: ConversationMessageRepository;
  readonly conversationSummaries: ConversationSummaryRepository;
  readonly contextPackageSnapshots: ContextPackageSnapshotRepository;
  readonly dataSources: DataSourceRepository;
  readonly fileAssetRefs: FileAssetRefRepository;
  readonly fileAssets: FileAssetRepository;
  readonly interactions: InteractionRepository;
  readonly longTermMemories: LongTermMemoryRepository;
  readonly queryHistory: QueryHistoryRepository;
  readonly protocolStates: ProtocolStateSnapshotRepository;
  readonly runEvents: RunEventRepository;
  readonly runs: RunRepository;
  readonly sessionBranches: SessionBranchRepository;
  readonly sessions: SessionRepository;
  readonly secrets: EncryptedSecretStore;
  readonly sqlAuditLogs: SqlAuditLogRepository;
  readonly traceSections: TraceSectionRepository;
  readonly userPasswordCredentials: UserPasswordCredentialRepository;
  readonly users: UserRepository;
  readonly workspaceMemberships: WorkspaceMembershipRepository;
  readonly workspaces: WorkspaceRepository;

  constructor(readonly db: DatabaseSync, secretMasterKey?: string) {
    this.users = new UserRepository(db);
    this.userPasswordCredentials = new UserPasswordCredentialRepository(db);
    this.authSessions = new AuthSessionRepository(db);
    this.authTokens = new AuthTokenRepository(db);
    this.authAuditEvents = new AuthAuditEventRepository(db);
    this.workspaces = new WorkspaceRepository(db);
    this.workspaceMemberships = new WorkspaceMembershipRepository(db);
    this.sessions = new SessionRepository(db);
    this.runs = new RunRepository(db);
    this.runEvents = new RunEventRepository(db);
    this.sessionBranches = new SessionBranchRepository(db);
    this.conversationMessages = new ConversationMessageRepository(db);
    this.conversationSummaries = new ConversationSummaryRepository(db);
    this.artifacts = new ArtifactRepository(db);
    this.artifactVersions = new ArtifactVersionRepository(db);
    this.configJobs = new ConfigJobRepository(db);
    this.configResources = new ConfigResourceRepository(db);
    this.checkpoints = new CheckpointRepository(db);
    this.traceSections = new TraceSectionRepository(db);
    this.contextPackageSnapshots = new ContextPackageSnapshotRepository(db);
    this.dataSources = new DataSourceRepository(db);
    this.fileAssets = new FileAssetRepository(db);
    this.fileAssetRefs = new FileAssetRefRepository(db);
    this.interactions = new InteractionRepository(db);
    this.longTermMemories = new LongTermMemoryRepository(db);
    this.queryHistory = new QueryHistoryRepository(db);
    this.protocolStates = new ProtocolStateSnapshotRepository(db);
    this.secrets = new EncryptedSecretStore(db, secretMasterKey);
    this.sqlAuditLogs = new SqlAuditLogRepository(db);
  }

  close(): void {
    this.db.close();
  }
}

export class InteractionRepository {
  constructor(private readonly db: DatabaseSync) {}

  request(input: {
    id: string;
    user_id: string;
    session_id: string;
    run_id: string;
    tool_call_id: string;
    tool_name: InteractionRecord["tool_name"];
    payload: unknown;
    interrupt_event?: unknown;
  }): InteractionRecord {
    const createdAt = new Date().toISOString();
    const interruptEventJson =
      input.interrupt_event === undefined
        ? null
        : typeof input.interrupt_event === "string"
          ? input.interrupt_event
          : JSON.stringify(input.interrupt_event);
    this.db.prepare(`
      INSERT INTO interactions (
        id, user_id, session_id, run_id, tool_call_id, tool_name, payload_json, interrupt_event_json, status, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
      ON CONFLICT(user_id, run_id, tool_call_id) DO NOTHING
    `).run(
      input.id,
      input.user_id,
      input.session_id,
      input.run_id,
      input.tool_call_id,
      input.tool_name,
      JSON.stringify(input.payload),
      interruptEventJson,
      createdAt
    );
    return this.getByToolCall(input);
  }

  listPendingBySession(input: { user_id: string; session_id: string }): InteractionRecord[] {
    return this.db
      .prepare(`
        SELECT *
        FROM interactions
        WHERE user_id = ?
          AND session_id = ?
          AND status = 'pending'
        ORDER BY created_at ASC
      `)
      .all(input.user_id, input.session_id)
      .map((row) => mapInteractionRow(row))
      .filter((record): record is InteractionRecord => Boolean(record));
  }

  /**
   * All interactions for a session regardless of status. Used by conversation restore
   * to authoritatively recover HITL tool names (ask_user / submit_plan) even after the
   * interaction has been resolved (R-018).
   */
  listBySession(input: { user_id: string; session_id: string }): InteractionRecord[] {
    return this.db
      .prepare(`
        SELECT *
        FROM interactions
        WHERE user_id = ?
          AND session_id = ?
        ORDER BY created_at ASC
      `)
      .all(input.user_id, input.session_id)
      .map((row) => mapInteractionRow(row))
      .filter((record): record is InteractionRecord => Boolean(record));
  }

  getByToolCall(input: { user_id: string; run_id: string; tool_call_id: string }): InteractionRecord {
    const interaction = mapInteractionRow(
      this.db.prepare(
        "SELECT * FROM interactions WHERE user_id = ? AND run_id = ? AND tool_call_id = ?"
      ).get(input.user_id, input.run_id, input.tool_call_id)
    );
    if (!interaction) {
      throw new Error(`INTERACTION_NOT_FOUND:${input.tool_call_id}`);
    }
    return interaction;
  }

  resolve(input: {
    user_id: string;
    run_id: string;
    tool_call_id: string;
    resume_fingerprint: string;
    response: unknown;
  }): InteractionRecord {
    const current = this.getByToolCall(input);
    if (current.status === "resolved") {
      if (current.resume_fingerprint !== input.resume_fingerprint) {
        throw new Error(`INTERACTION_RESUME_MISMATCH:${input.tool_call_id}`);
      }
      return current;
    }
    const resolvedAt = new Date().toISOString();
    this.db.prepare(`
      UPDATE interactions
      SET status = 'resolved', resume_fingerprint = ?, response_json = ?, resolved_at = ?
      WHERE user_id = ? AND run_id = ? AND tool_call_id = ? AND status = 'pending'
    `).run(
      input.resume_fingerprint,
      JSON.stringify(input.response),
      resolvedAt,
      input.user_id,
      input.run_id,
      input.tool_call_id
    );
    return this.getByToolCall(input);
  }

  cancel(input: {
    user_id: string;
    run_id: string;
    tool_call_id: string;
    resume_fingerprint: string;
  }): InteractionRecord {
    const resolvedAt = new Date().toISOString();
    this.db.prepare(`
      UPDATE interactions
      SET status = 'canceled', resume_fingerprint = ?, response_json = 'false', resolved_at = ?
      WHERE user_id = ? AND run_id = ? AND tool_call_id = ? AND status = 'pending'
    `).run(
      input.resume_fingerprint,
      resolvedAt,
      input.user_id,
      input.run_id,
      input.tool_call_id
    );
    return this.getByToolCall(input);
  }
}

export class UserRepository {
  constructor(private readonly db: DatabaseSync) {}

  createPasswordUser(input: { id: string; email: string; display_name?: string }): UserRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO users (id, email, display_name, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?)
    `).run(input.id, input.email.toLowerCase(), input.display_name ?? null, now, now);
    return this.getById({ user_id: input.id });
  }

  upsertDevUser(input: { id: string; email: string; display_name: string; dev_token: string }): UserRecord {
    const now = new Date().toISOString();

    this.db
      .prepare(
        `
        INSERT INTO users (id, email, display_name, dev_token, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          email = excluded.email,
          display_name = excluded.display_name,
          dev_token = excluded.dev_token,
          updated_at = excluded.updated_at
      `
      )
      .run(input.id, input.email, input.display_name, input.dev_token, now, now);

    return this.getById({ user_id: input.id });
  }

  getByDevToken(input: { dev_token: string }): Optional<UserRecord> {
    return mapUserRow(this.db.prepare("SELECT * FROM users WHERE dev_token = ?").get(input.dev_token));
  }

  findByEmail(input: { email: string }): Optional<UserRecord> {
    return mapUserRow(this.db.prepare("SELECT * FROM users WHERE lower(email) = lower(?)").get(input.email));
  }

  markEmailVerified(input: { user_id: string; verified_at?: string }): UserRecord {
    const verifiedAt = input.verified_at ?? new Date().toISOString();
    this.db.prepare(`
      UPDATE users SET email_verified_at = ?, updated_at = ? WHERE id = ?
    `).run(verifiedAt, verifiedAt, input.user_id);
    return this.getById(input);
  }

  touchPasswordUpdated(input: { user_id: string; updated_at?: string }): UserRecord {
    const updatedAt = input.updated_at ?? new Date().toISOString();
    this.db.prepare(`
      UPDATE users SET password_updated_at = ?, updated_at = ? WHERE id = ?
    `).run(updatedAt, updatedAt, input.user_id);
    return this.getById(input);
  }

  list(): UserRecord[] {
    return this.db
      .prepare("SELECT * FROM users ORDER BY updated_at DESC")
      .all()
      .map((row) => {
        const user = mapUserRow(row);
        if (!user) {
          throw new Error("Invalid user row");
        }
        return user;
      });
  }

  getById(input: { user_id: string }): UserRecord {
    const user = mapUserRow(this.db.prepare("SELECT * FROM users WHERE id = ?").get(input.user_id));

    if (!user) {
      throw new Error(`User not found: ${input.user_id}`);
    }

    return user;
  }
}

export class UserPasswordCredentialRepository {
  constructor(private readonly db: DatabaseSync) {}

  set(input: { user_id: string; password_hash: string; password_hash_params: string }): UserPasswordCredentialRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO user_password_credentials (user_id, password_hash, password_hash_params, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(user_id) DO UPDATE SET
        password_hash = excluded.password_hash,
        password_hash_params = excluded.password_hash_params,
        updated_at = excluded.updated_at
    `).run(input.user_id, input.password_hash, input.password_hash_params, now, now);
    return this.get({ user_id: input.user_id });
  }

  find(input: { user_id: string }): Optional<UserPasswordCredentialRecord> {
    return mapUserPasswordCredentialRow(
      this.db.prepare("SELECT * FROM user_password_credentials WHERE user_id = ?").get(input.user_id)
    );
  }

  get(input: { user_id: string }): UserPasswordCredentialRecord {
    const credential = this.find(input);
    if (!credential) {
      throw new Error(`USER_PASSWORD_CREDENTIAL_NOT_FOUND:${input.user_id}`);
    }
    return credential;
  }
}

export class AuthSessionRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: {
    id: string;
    user_id: string;
    token_hash: string;
    csrf_token_hash: string;
    expires_at: string;
    ip_address?: string;
    user_agent?: string;
  }): AuthSessionRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO auth_sessions (
        id, user_id, token_hash, csrf_token_hash, user_agent, ip_address,
        expires_at, created_at, last_seen_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      input.id,
      input.user_id,
      input.token_hash,
      input.csrf_token_hash,
      input.user_agent ?? null,
      input.ip_address ?? null,
      input.expires_at,
      now,
      now
    );
    return this.get({ id: input.id });
  }

  findByTokenHash(input: { token_hash: string; now?: string }): Optional<AuthSessionRecord> {
    const now = input.now ?? new Date().toISOString();
    return mapAuthSessionRow(this.db.prepare(`
      SELECT * FROM auth_sessions
      WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?
    `).get(input.token_hash, now));
  }

  get(input: { id: string }): AuthSessionRecord {
    const session = mapAuthSessionRow(this.db.prepare("SELECT * FROM auth_sessions WHERE id = ?").get(input.id));
    if (!session) {
      throw new Error(`AUTH_SESSION_NOT_FOUND:${input.id}`);
    }
    return session;
  }

  listByUser(input: { user_id: string }): AuthSessionRecord[] {
    return this.db.prepare(`
      SELECT * FROM auth_sessions
      WHERE user_id = ? AND revoked_at IS NULL AND expires_at > ?
      ORDER BY last_seen_at DESC
    `).all(input.user_id, new Date().toISOString())
      .map(mapAuthSessionRow)
      .filter((record): record is AuthSessionRecord => Boolean(record));
  }

  touch(input: { id: string; last_seen_at?: string }): void {
    this.db.prepare("UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?")
      .run(input.last_seen_at ?? new Date().toISOString(), input.id);
  }

  rotateCsrf(input: { id: string; csrf_token_hash: string }): AuthSessionRecord {
    const result = this.db.prepare(`
      UPDATE auth_sessions
      SET csrf_token_hash = ?, last_seen_at = ?
      WHERE id = ? AND revoked_at IS NULL AND expires_at > ?
    `).run(input.csrf_token_hash, new Date().toISOString(), input.id, new Date().toISOString());
    if (result.changes !== 1) {
      throw new Error(`AUTH_SESSION_CSRF_ROTATE_FAILED:${input.id}`);
    }
    return this.get({ id: input.id });
  }

  revoke(input: { id: string; revoked_at?: string }): void {
    this.db.prepare("UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?")
      .run(input.revoked_at ?? new Date().toISOString(), input.id);
  }

  revokeByUser(input: { user_id: string; except_session_id?: string; revoked_at?: string }): void {
    const revokedAt = input.revoked_at ?? new Date().toISOString();
    if (input.except_session_id) {
      this.db.prepare(`
        UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, ?)
        WHERE user_id = ? AND id <> ? AND revoked_at IS NULL
      `).run(revokedAt, input.user_id, input.except_session_id);
      return;
    }
    this.db.prepare(`
      UPDATE auth_sessions SET revoked_at = COALESCE(revoked_at, ?)
      WHERE user_id = ? AND revoked_at IS NULL
    `).run(revokedAt, input.user_id);
  }
}

export class AuthTokenRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: {
    id: string;
    user_id: string;
    purpose: AuthTokenPurpose;
    token_hash: string;
    expires_at: string;
  }): AuthTokenRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO auth_tokens (id, user_id, purpose, token_hash, expires_at, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
    `).run(input.id, input.user_id, input.purpose, input.token_hash, input.expires_at, now);
    return this.get({ id: input.id });
  }

  findValid(input: { purpose: AuthTokenPurpose; token_hash: string; now?: string }): Optional<AuthTokenRecord> {
    return mapAuthTokenRow(this.db.prepare(`
      SELECT * FROM auth_tokens
      WHERE purpose = ? AND token_hash = ? AND consumed_at IS NULL AND expires_at > ?
    `).get(input.purpose, input.token_hash, input.now ?? new Date().toISOString()));
  }

  consume(input: { id: string; consumed_at?: string }): AuthTokenRecord {
    const consumedAt = input.consumed_at ?? new Date().toISOString();
    this.db.prepare("UPDATE auth_tokens SET consumed_at = COALESCE(consumed_at, ?) WHERE id = ?")
      .run(consumedAt, input.id);
    return this.get(input);
  }

  get(input: { id: string }): AuthTokenRecord {
    const token = mapAuthTokenRow(this.db.prepare("SELECT * FROM auth_tokens WHERE id = ?").get(input.id));
    if (!token) {
      throw new Error(`AUTH_TOKEN_NOT_FOUND:${input.id}`);
    }
    return token;
  }
}

export class WorkspaceRepository {
  constructor(private readonly db: DatabaseSync) {}

  createPersonal(input: { id: string; owner_user_id: string; name: string }): WorkspaceRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO workspaces (id, name, kind, owner_user_id, created_at, updated_at)
      VALUES (?, ?, 'personal', ?, ?, ?)
      ON CONFLICT(id) DO UPDATE SET
        name = excluded.name,
        updated_at = excluded.updated_at
    `).run(input.id, input.name, input.owner_user_id, now, now);
    return this.get({ id: input.id });
  }

  get(input: { id: string }): WorkspaceRecord {
    const workspace = mapWorkspaceRow(this.db.prepare("SELECT * FROM workspaces WHERE id = ?").get(input.id));
    if (!workspace) {
      throw new Error(`WORKSPACE_NOT_FOUND:${input.id}`);
    }
    return workspace;
  }

  findPersonalByUser(input: { user_id: string }): Optional<WorkspaceRecord> {
    return mapWorkspaceRow(this.db.prepare(`
      SELECT * FROM workspaces WHERE kind = 'personal' AND owner_user_id = ? ORDER BY created_at ASC LIMIT 1
    `).get(input.user_id));
  }
}

export class WorkspaceMembershipRepository {
  constructor(private readonly db: DatabaseSync) {}

  upsertOwner(input: { workspace_id: string; user_id: string }): WorkspaceMembershipRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO workspace_memberships (workspace_id, user_id, role, created_at)
      VALUES (?, ?, 'owner', ?)
      ON CONFLICT(workspace_id, user_id) DO UPDATE SET role = 'owner'
    `).run(input.workspace_id, input.user_id, now);
    return this.get(input);
  }

  get(input: { workspace_id: string; user_id: string }): WorkspaceMembershipRecord {
    const membership = mapWorkspaceMembershipRow(this.db.prepare(`
      SELECT * FROM workspace_memberships WHERE workspace_id = ? AND user_id = ?
    `).get(input.workspace_id, input.user_id));
    if (!membership) {
      throw new Error(`WORKSPACE_MEMBERSHIP_NOT_FOUND:${input.workspace_id}:${input.user_id}`);
    }
    return membership;
  }
}

export class AuthAuditEventRepository {
  constructor(private readonly db: DatabaseSync) {}

  append(input: {
    id: string;
    event_type: string;
    email?: string;
    ip_address?: string;
    metadata?: unknown;
    user_agent?: string;
    user_id?: string;
  }): AuthAuditEventRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO auth_audit_events (
        id, user_id, email, event_type, ip_address, user_agent, metadata_json, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      input.id,
      input.user_id ?? null,
      input.email ?? null,
      input.event_type,
      input.ip_address ?? null,
      input.user_agent ?? null,
      input.metadata === undefined ? null : JSON.stringify(input.metadata),
      now
    );
    return this.get({ id: input.id });
  }

  get(input: { id: string }): AuthAuditEventRecord {
    const event = mapAuthAuditEventRow(this.db.prepare("SELECT * FROM auth_audit_events WHERE id = ?").get(input.id));
    if (!event) {
      throw new Error(`AUTH_AUDIT_EVENT_NOT_FOUND:${input.id}`);
    }
    return event;
  }
}

export class SessionRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateSessionInput): SessionRecord {
    const now = new Date().toISOString();

    this.db
      .prepare(
        `
        INSERT INTO sessions (
          id, user_id, title, title_source, selected_datasource_id, selected_collection_id,
          created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, id) DO UPDATE SET
          title = COALESCE(excluded.title, sessions.title),
          title_source = COALESCE(excluded.title_source, sessions.title_source),
          selected_datasource_id = COALESCE(excluded.selected_datasource_id, sessions.selected_datasource_id),
          selected_collection_id = COALESCE(excluded.selected_collection_id, sessions.selected_collection_id),
          updated_at = excluded.updated_at
      `
      )
      .run(
        input.id,
        input.user_id,
        input.title ?? null,
        input.title_source ?? null,
        input.selected_datasource_id ?? null,
        input.selected_collection_id ?? null,
        now,
        now
      );

    return this.get({ user_id: input.user_id, session_id: input.id });
  }

  get(input: { user_id: string; session_id: string }): SessionRecord {
    const session = mapSessionRow(
      this.db.prepare("SELECT * FROM sessions WHERE user_id = ? AND id = ?").get(input.user_id, input.session_id)
    );

    if (!session) {
      throw new Error(`Session not found: ${input.session_id}`);
    }

    return session;
  }

  list(input: { cursor?: string; limit?: number; user_id: string }): SessionRecord[] {
    const limit = Math.max(1, Math.min(input.limit ?? 50, 200));
    if (input.cursor) {
      const cursor = decodeSessionCursor(input.cursor);
      if (cursor) {
        return this.db
          .prepare(`
            SELECT * FROM sessions
            WHERE user_id = ?
              AND (
                COALESCE(last_message_at, updated_at) < ?
                OR (COALESCE(last_message_at, updated_at) = ? AND id < ?)
              )
            ORDER BY COALESCE(last_message_at, updated_at) DESC, id DESC
            LIMIT ?
          `)
          .all(input.user_id, cursor.sort_at, cursor.sort_at, cursor.id, limit)
          .map(mapRequiredSessionRow);
      }
    }
    return this.db
      .prepare(`
        SELECT * FROM sessions
        WHERE user_id = ?
        ORDER BY COALESCE(last_message_at, updated_at) DESC, id DESC
        LIMIT ?
      `)
      .all(input.user_id, limit)
      .map(mapRequiredSessionRow);
  }

  touchLastMessage(input: { last_message_at?: string; session_id: string; user_id: string }): SessionRecord {
    const now = new Date().toISOString();
    const lastMessageAt = input.last_message_at ?? now;
    this.db
      .prepare(`
        UPDATE sessions
        SET last_message_at = ?, updated_at = ?
        WHERE user_id = ? AND id = ?
      `)
      .run(lastMessageAt, now, input.user_id, input.session_id);
    return this.get({ user_id: input.user_id, session_id: input.session_id });
  }

  updateTitle(input: {
    session_id: string;
    title: string;
    title_source: "llm" | "fallback" | "user";
    user_id: string;
  }): SessionRecord {
    const title = input.title.trim();
    if (!title) {
      throw new Error("SESSION_TITLE_REQUIRED");
    }
    const now = new Date().toISOString();
    this.db
      .prepare(`
        UPDATE sessions
        SET title = ?, title_source = ?, updated_at = ?
        WHERE user_id = ? AND id = ?
      `)
      .run(title, input.title_source, now, input.user_id, input.session_id);
    return this.get({ user_id: input.user_id, session_id: input.session_id });
  }

  updateAutoTitleIfAllowed(input: {
    session_id: string;
    title: string;
    title_source: "llm" | "fallback";
    user_id: string;
  }): Optional<SessionRecord> {
    const title = input.title.trim();
    if (!title) {
      return undefined;
    }
    const now = new Date().toISOString();
    const result = this.db
      .prepare(`
        UPDATE sessions
        SET title = ?, title_source = ?, updated_at = ?
        WHERE user_id = ? AND id = ? AND COALESCE(title_source, '') != 'user'
          AND (title IS NULL OR title = '' OR COALESCE(title_source, '') != 'llm')
      `)
      .run(title, input.title_source, now, input.user_id, input.session_id);
    return result.changes === 1 ? this.get({ user_id: input.user_id, session_id: input.session_id }) : undefined;
  }

  /**
   * Permanently delete a session and its dependent rows (runs, conversation,
   * artifacts, branches, etc.). Also cascades to descendant branch sessions.
   * Idempotent: missing sessions are a no-op (returns deleted: false).
   */
  delete(input: { user_id: string; session_id: string }): { deleted: boolean; deletedSessionIds: string[] } {
    const existing = mapSessionRow(
      this.db.prepare("SELECT * FROM sessions WHERE user_id = ? AND id = ?").get(input.user_id, input.session_id)
    );
    if (!existing) {
      return { deleted: false, deletedSessionIds: [] };
    }

    const sessionIds = collectDescendantSessionIds(this.db, input.user_id, input.session_id);
    sessionIds.push(input.session_id);

    this.db.exec("BEGIN IMMEDIATE");
    try {
      const placeholders = sessionIds.map(() => "?").join(", ");
      const scope = [input.user_id, ...sessionIds];
      const runIds = this.db.prepare(`
        SELECT id FROM runs WHERE user_id = ? AND session_id IN (${placeholders})
      `).all(...scope)
        .map((row) => (isRecord(row) && typeof row.id === "string" ? row.id : null))
        .filter((id): id is string => Boolean(id));
      const runPlaceholders = runIds.map(() => "?").join(", ");

      this.db.prepare(`
        DELETE FROM session_branches
        WHERE user_id = ?
          AND (
            child_session_id IN (${placeholders})
            OR parent_session_id IN (${placeholders})
            OR root_session_id IN (${placeholders})
          )
      `).run(input.user_id, ...sessionIds, ...sessionIds, ...sessionIds);

      this.db.prepare(`
        DELETE FROM artifact_versions
        WHERE user_id = ?
          AND artifact_id IN (
            SELECT id FROM artifacts WHERE user_id = ? AND session_id IN (${placeholders})
          )
      `).run(input.user_id, input.user_id, ...sessionIds);

      this.db.prepare(`
        DELETE FROM artifacts WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        UPDATE checkpoints
        SET parent_checkpoint_id = NULL
        WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM checkpoints WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM trace_sections WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      if (runIds.length > 0) {
        this.db.prepare(`
          DELETE FROM protocol_state_snapshots
          WHERE user_id = ? AND run_id IN (${runPlaceholders})
        `).run(input.user_id, ...runIds);
        this.db.prepare(`
          DELETE FROM protocol_event_journal
          WHERE user_id = ? AND run_id IN (${runPlaceholders})
        `).run(input.user_id, ...runIds);
      }

      this.db.prepare(`
        DELETE FROM context_package_snapshots WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM conversation_messages WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM conversation_summaries WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM interactions WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM query_history WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM long_term_memories WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM run_events WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      if (runIds.length > 0) {
        this.db.prepare(`
          DELETE FROM sql_audit_logs
          WHERE user_id = ? AND run_id IN (${runPlaceholders})
        `).run(input.user_id, ...runIds);
        this.db.prepare(`
          UPDATE runs
          SET parent_run_id = NULL
          WHERE user_id = ? AND parent_run_id IN (${runPlaceholders})
        `).run(input.user_id, ...runIds);
      }

      this.db.prepare(`
        UPDATE runs
        SET parent_run_id = NULL
        WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM runs WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM file_asset_refs WHERE user_id = ? AND session_id IN (${placeholders})
      `).run(...scope);

      this.db.prepare(`
        DELETE FROM sessions WHERE user_id = ? AND id IN (${placeholders})
      `).run(...scope);

      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }

    return { deleted: true, deletedSessionIds: sessionIds };
  }
}

function collectDescendantSessionIds(db: DatabaseSync, userId: string, sessionId: string): string[] {
  const ordered: string[] = [];
  const seen = new Set<string>();
  const stack = [sessionId];

  while (stack.length > 0) {
    const current = stack.pop() as string;
    const children = db.prepare(`
      SELECT child_session_id
      FROM session_branches
      WHERE user_id = ? AND parent_session_id = ?
      ORDER BY created_at ASC, child_session_id ASC
    `).all(userId, current);

    for (const row of children) {
      if (typeof row !== "object" || row === null) continue;
      const childId = (row as { child_session_id?: unknown }).child_session_id;
      if (typeof childId !== "string") continue;
      if (seen.has(childId) || childId === sessionId) continue;
      seen.add(childId);
      stack.push(childId);
      ordered.push(childId);
    }
  }

  return ordered;
}

export class SessionBranchRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateSessionBranchInput): SessionBranchRecord {
    const createdAt = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO session_branches (
        id, user_id, child_session_id, parent_session_id, root_session_id,
        fork_run_id, fork_checkpoint_id, fork_message_end_position, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      input.id,
      input.user_id,
      input.child_session_id,
      input.parent_session_id,
      input.root_session_id,
      input.fork_run_id,
      input.fork_checkpoint_id ?? null,
      input.fork_message_end_position,
      createdAt
    );
    return this.getByChild({ user_id: input.user_id, child_session_id: input.child_session_id });
  }

  findByChild(input: { user_id: string; child_session_id: string }): Optional<SessionBranchRecord> {
    return mapSessionBranchRow(
      this.db.prepare(`
        SELECT *
        FROM session_branches
        WHERE user_id = ? AND child_session_id = ?
      `).get(input.user_id, input.child_session_id)
    );
  }

  getByChild(input: { user_id: string; child_session_id: string }): SessionBranchRecord {
    const branch = this.findByChild(input);
    if (!branch) {
      throw new Error(`SESSION_BRANCH_NOT_FOUND:${input.child_session_id}`);
    }
    return branch;
  }

  listChildrenForFork(input: {
    user_id: string;
    parent_session_id: string;
    fork_run_id: string;
  }): SessionBranchRecord[] {
    return this.db.prepare(`
      SELECT *
      FROM session_branches
      WHERE user_id = ?
        AND parent_session_id = ?
        AND fork_run_id = ?
      ORDER BY created_at ASC, child_session_id ASC
    `).all(input.user_id, input.parent_session_id, input.fork_run_id)
      .map(mapRequiredSessionBranchRow);
  }

  listChildrenForParents(input: { user_id: string; parent_session_ids: string[] }): SessionBranchRecord[] {
    const parentIds = [...new Set(input.parent_session_ids)].filter((id) => id.length > 0);
    if (parentIds.length === 0) {
      return [];
    }
    const placeholders = parentIds.map(() => "?").join(", ");
    return this.db.prepare(`
      SELECT *
      FROM session_branches
      WHERE user_id = ?
        AND parent_session_id IN (${placeholders})
      ORDER BY created_at ASC, child_session_id ASC
    `).all(input.user_id, ...parentIds)
      .map(mapRequiredSessionBranchRow);
  }
}

export class DataSourceRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateDataSourceInput): DataSourceRecord {
    const now = new Date().toISOString();
    const current = this.find({ user_id: input.user_id, datasource_id: input.id });
    if (input.expected_revision !== undefined && current?.revision !== input.expected_revision) {
      throw new Error(`REVISION_CONFLICT:${input.id}`);
    }
    const revision = current ? current.revision + 1 : 1;

    this.db
      .prepare(
        `
        INSERT INTO data_sources (
          id, user_id, name, type, config_json, credential_ref, description, status, revision, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, id) DO UPDATE SET
          name = excluded.name,
          type = excluded.type,
          config_json = excluded.config_json,
          credential_ref = excluded.credential_ref,
          description = excluded.description,
          status = excluded.status,
          revision = excluded.revision,
          updated_at = excluded.updated_at
      `
      )
      .run(
        input.id,
        input.user_id,
        input.name,
        input.type,
        JSON.stringify(input.config),
        input.credential_ref ?? null,
        input.description ?? null,
        input.status ?? "ready",
        revision,
        now,
        now
      );

    return this.get({ user_id: input.user_id, datasource_id: input.id });
  }

  /** Find one datasource without throwing. */
  find(input: { user_id: string; datasource_id: string }): DataSourceRecord | undefined {
    return mapDataSourceRow(
      this.db.prepare("SELECT * FROM data_sources WHERE user_id = ? AND id = ?")
        .get(input.user_id, input.datasource_id)
    );
  }

  get(input: { user_id: string; datasource_id: string }): DataSourceRecord {
    const dataSource = mapDataSourceRow(
      this.db
        .prepare("SELECT * FROM data_sources WHERE user_id = ? AND id = ?")
        .get(input.user_id, input.datasource_id)
    );

    if (!dataSource) {
      throw new Error(`Data source not found: ${input.datasource_id}`);
    }

    return dataSource;
  }

  list(input: { user_id: string; enabled_only?: boolean }): DataSourceRecord[] {
    const sql = input.enabled_only
      ? "SELECT * FROM data_sources WHERE user_id = ? AND status = 'ready' ORDER BY updated_at DESC"
      : "SELECT * FROM data_sources WHERE user_id = ? AND status <> 'deleted' ORDER BY updated_at DESC";

    return this.db.prepare(sql).all(input.user_id).map(mapRequiredDataSourceRow);
  }

  touchTest(input: { user_id: string; datasource_id: string; status: DataSourceRecord["status"] }): DataSourceRecord {
    const now = new Date().toISOString();

    this.db
      .prepare(
        `
        UPDATE data_sources
        SET status = ?, last_test_at = ?, updated_at = ?
        WHERE user_id = ? AND id = ?
      `
      )
      .run(input.status, now, now, input.user_id, input.datasource_id);

    return this.get({ user_id: input.user_id, datasource_id: input.datasource_id });
  }

  /** Tombstone one datasource while preserving SQL audit foreign-key history. */
  delete(input: { user_id: string; datasource_id: string }): void {
    const result = this.db.prepare(`
      UPDATE data_sources
      SET status = 'deleted', credential_ref = NULL, revision = revision + 1, updated_at = ?
      WHERE user_id = ? AND id = ? AND status <> 'deleted'
    `).run(new Date().toISOString(), input.user_id, input.datasource_id);
    if (result.changes !== 1) {
      throw new Error(`DATASOURCE_NOT_FOUND:${input.datasource_id}`);
    }
  }
}

export class RunRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateRunInput): RunRecord {
    const result = this.claim(input);

    if (!result.created) {
      throw new Error(`Run already exists: ${input.id}`);
    }

    return result.run;
  }

  claim(input: CreateRunInput): ClaimRunResult {
    const now = new Date().toISOString();

    const result = this.db
      .prepare(
        `
        INSERT INTO runs (
          id, user_id, session_id, parent_run_id, request_fingerprint, status, user_input,
          model_provider, model_name, datasource_id, collection_id, started_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, id) DO NOTHING
      `
      )
      .run(
        input.id,
        input.user_id,
        input.session_id,
        input.parent_run_id ?? null,
        input.request_fingerprint ?? null,
        input.status ?? "queued",
        input.user_input,
        input.model_provider ?? null,
        input.model_name ?? null,
        input.datasource_id ?? null,
        input.collection_id ?? null,
        now
      );

    return {
      created: result.changes === 1,
      run: this.get({ user_id: input.user_id, run_id: input.id })
    };
  }

  find(input: { user_id: string; run_id: string }): Optional<RunRecord> {
    return mapRunRow(
      this.db.prepare("SELECT * FROM runs WHERE user_id = ? AND id = ?").get(input.user_id, input.run_id)
    );
  }

  findActiveBySession(input: { exclude_run_id?: string; session_id: string; user_id: string }): Optional<RunRecord> {
    const params: Array<string> = [input.user_id, input.session_id];
    let sql = `
      SELECT *
      FROM runs
      WHERE user_id = ?
        AND session_id = ?
        AND status IN ('queued', 'running', 'suspended')
    `;

    if (input.exclude_run_id) {
      sql += " AND id <> ?";
      params.push(input.exclude_run_id);
    }

    sql += " ORDER BY started_at DESC LIMIT 1";
    return mapRunRow(this.db.prepare(sql).get(...params));
  }

  listByStatuses(input: { statuses: Array<RunRecord["status"]>; limit?: number }): RunRecord[] {
    if (input.statuses.length === 0) {
      return [];
    }
    const limit = Math.max(1, Math.min(input.limit ?? 500, 2000));
    const placeholders = input.statuses.map(() => "?").join(", ");
    const rows = this.db
      .prepare(
        `
        SELECT *
        FROM runs
        WHERE status IN (${placeholders})
        ORDER BY started_at DESC
        LIMIT ?
      `
      )
      .all(...input.statuses, limit);
    return rows
      .map((row) => mapRunRow(row))
      .filter((run): run is RunRecord => run !== undefined);
  }

  get(input: { user_id: string; run_id: string }): RunRecord {
    const run = this.find(input);

    if (!run) {
      throw new Error(`Run not found: ${input.run_id}`);
    }

    return run;
  }

  updateStatus(input: {
    user_id: string;
    run_id: string;
    status: RunRecord["status"];
    error_message?: string;
  }): RunRecord {
    const finishedAt = ["completed", "failed", "canceled"].includes(input.status) ? new Date().toISOString() : null;

    this.db
      .prepare(
        `
        UPDATE runs
        SET status = ?, finished_at = ?, error_message = ?
        WHERE user_id = ? AND id = ?
      `
      )
      .run(input.status, finishedAt, input.error_message ?? null, input.user_id, input.run_id);

    return this.get({ user_id: input.user_id, run_id: input.run_id });
  }
}

export class RunEventRepository {
  constructor(private readonly db: DatabaseSync) {}

  append(input: WriteRunEventInput): RunEventRecord {
    const protocolEventId = protocolEventIdFromBaseEvent(input.event);
    if (protocolEventId) {
      const existing = mapRunEventRow(this.db.prepare(`
        SELECT * FROM run_events
        WHERE user_id = ? AND run_id = ?
          AND json_extract(payload_json, '$.value.eventId') = ?
        LIMIT 1
      `).get(input.user_id, input.run_id, protocolEventId));
      if (existing) {
        return existing;
      }
    }
    const seq = this.nextSeq({ user_id: input.user_id, run_id: input.run_id });
    const createdAt = new Date().toISOString();
    const id = `${input.run_id}:${seq}`;

    this.db
      .prepare(
        `
        INSERT INTO run_events (id, user_id, run_id, session_id, seq, event_type, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      `
      )
      .run(
        id,
        input.user_id,
        input.run_id,
        input.session_id,
        seq,
        input.event.type,
        JSON.stringify(input.event),
        createdAt
      );

    return this.getBySeq({ user_id: input.user_id, run_id: input.run_id, seq });
  }

  listByRun(input: { user_id: string; run_id: string }): RunEventRecord[] {
    return this.db
      .prepare("SELECT * FROM run_events WHERE user_id = ? AND run_id = ? ORDER BY seq ASC")
      .all(input.user_id, input.run_id)
      .map(mapRequiredRunEventRow);
  }

  private getBySeq(input: { user_id: string; run_id: string; seq: number }): RunEventRecord {
    const event = mapRunEventRow(
      this.db
        .prepare("SELECT * FROM run_events WHERE user_id = ? AND run_id = ? AND seq = ?")
        .get(input.user_id, input.run_id, input.seq)
    );

    if (!event) {
      throw new Error(`Run event not found: ${input.run_id}:${input.seq}`);
    }

    return event;
  }

  private nextSeq(input: { user_id: string; run_id: string }): number {
    const row = this.db
      .prepare("SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM run_events WHERE user_id = ? AND run_id = ?")
      .get(input.user_id, input.run_id);

    if (!isRecord(row) || typeof row.next_seq !== "number") {
      throw new Error(`Unable to allocate event seq for run: ${input.run_id}`);
    }

    return row.next_seq;
  }
}

const protocolEventIdFromBaseEvent = (event: BaseEvent): string | undefined => {
  if (event.type !== EventType.CUSTOM || !isRecord(event) || !isRecord(event.value)) {
    return undefined;
  }
  return typeof event.value.eventId === "string" && event.value.eventId.length > 0
    ? event.value.eventId
    : undefined;
};

export class ConversationMessageRepository {
  constructor(private readonly db: DatabaseSync) {}

  append(input: CreateConversationMessageInput): ConversationMessageRecord {
    const createdAt = new Date().toISOString();
    const contentJson = JSON.stringify(input.content ?? { text: input.content_text });
    // Include message_id when present so empty tool-parent rows (same role +
    // empty content_text, different AG-UI message ids) do not collide on
    // idx_conversation_messages_run_hash.
    const contentHash = createHash("sha256")
      .update(
        JSON.stringify({
          role: input.role,
          content_text: input.content_text,
          ...(input.message_id ? { message_id: input.message_id } : {})
        })
      )
      .digest("hex");
    const position = this.nextPosition({ user_id: input.user_id, session_id: input.session_id });

    const result = this.db
      .prepare(
        `
        INSERT OR IGNORE INTO conversation_messages (
          id, user_id, session_id, run_id, role, source, message_id,
          content_json, content_text, content_hash, position, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `
      )
      .run(
        input.id,
        input.user_id,
        input.session_id,
        input.run_id,
        input.role,
        input.source,
        input.message_id ?? null,
        contentJson,
        input.content_text,
        contentHash,
        position,
        createdAt
      );

    if (result.changes === 1) {
      return this.get({ user_id: input.user_id, message_record_id: input.id });
    }

    const existing = input.message_id
      ? this.findByMessageId({
        user_id: input.user_id,
        session_id: input.session_id,
        message_id: input.message_id
      })
      : undefined;

    return existing ?? this.getByRunHash({
      user_id: input.user_id,
      session_id: input.session_id,
      run_id: input.run_id,
      role: input.role,
      content_hash: contentHash
    });
  }

  get(input: { user_id: string; message_record_id: string }): ConversationMessageRecord {
    const message = mapConversationMessageRow(
      this.db
        .prepare("SELECT * FROM conversation_messages WHERE user_id = ? AND id = ?")
        .get(input.user_id, input.message_record_id)
    );

    if (!message) {
      throw new Error(`Conversation message not found: ${input.message_record_id}`);
    }

    return message;
  }

  listRecent(input: {
    user_id: string;
    session_id: string;
    limit: number;
    exclude_run_id?: string;
  }): ConversationMessageRecord[] {
    const limit = Math.max(0, Math.floor(input.limit));
    if (limit === 0) {
      return [];
    }
    const rows = input.exclude_run_id
      ? this.db
        .prepare(
          `
          SELECT * FROM conversation_messages
          WHERE user_id = ? AND session_id = ? AND run_id <> ?
          ORDER BY position DESC
          LIMIT ?
        `
        )
        .all(input.user_id, input.session_id, input.exclude_run_id, limit)
      : this.db
        .prepare(
          `
          SELECT * FROM conversation_messages
          WHERE user_id = ? AND session_id = ?
          ORDER BY position DESC
          LIMIT ?
        `
        )
        .all(input.user_id, input.session_id, limit);

    return rows.map(mapRequiredConversationMessageRow).reverse();
  }

  listBySessionRange(input: {
    user_id: string;
    session_id: string;
    max_position?: number;
    exclude_run_id?: string;
  }): ConversationMessageRecord[] {
    const params: Array<string | number> = [input.user_id, input.session_id];
    let sql = `
      SELECT *
      FROM conversation_messages
      WHERE user_id = ? AND session_id = ?
    `;
    if (input.max_position !== undefined) {
      sql += " AND position <= ?";
      params.push(Math.max(0, Math.floor(input.max_position)));
    }
    if (input.exclude_run_id) {
      sql += " AND run_id <> ?";
      params.push(input.exclude_run_id);
    }
    sql += " ORDER BY position ASC";
    return this.db.prepare(sql).all(...params).map(mapRequiredConversationMessageRow);
  }

  findLatestAssistantByRun(input: {
    user_id: string;
    session_id: string;
    run_id: string;
  }): ConversationMessageRecord | undefined {
    return mapConversationMessageRow(
      this.db
        .prepare(
          `
          SELECT * FROM conversation_messages
          WHERE user_id = ? AND session_id = ? AND run_id = ?
            AND role = 'assistant' AND message_id IS NOT NULL
          ORDER BY position DESC
          LIMIT 1
        `
        )
        .get(input.user_id, input.session_id, input.run_id)
    );
  }

  private findByMessageId(input: {
    user_id: string;
    session_id: string;
    message_id: string;
  }): Optional<ConversationMessageRecord> {
    return mapConversationMessageRow(
      this.db
        .prepare("SELECT * FROM conversation_messages WHERE user_id = ? AND session_id = ? AND message_id = ?")
        .get(input.user_id, input.session_id, input.message_id)
    );
  }

  private getByRunHash(input: {
    user_id: string;
    session_id: string;
    run_id: string;
    role: ConversationMessageRole;
    content_hash: string;
  }): ConversationMessageRecord {
    const message = mapConversationMessageRow(
      this.db
        .prepare(
          `
          SELECT * FROM conversation_messages
          WHERE user_id = ? AND session_id = ? AND run_id = ? AND role = ? AND content_hash = ?
        `
        )
        .get(input.user_id, input.session_id, input.run_id, input.role, input.content_hash)
    );

    if (!message) {
      throw new Error(`Conversation message conflict could not be resolved: ${input.run_id}`);
    }

    return message;
  }

  private nextPosition(input: { user_id: string; session_id: string }): number {
    const row = this.db
      .prepare(
        `
        SELECT COALESCE(MAX(position), 0) + 1 AS next_position
        FROM conversation_messages
        WHERE user_id = ? AND session_id = ?
      `
      )
      .get(input.user_id, input.session_id);

    if (!isRecord(row) || typeof row.next_position !== "number") {
      throw new Error(`Unable to allocate conversation message position for session: ${input.session_id}`);
    }

    return row.next_position;
  }
}

export class ConversationSummaryRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateConversationSummaryInput): ConversationSummaryRecord {
    const createdAt = new Date().toISOString();
    const summaryHash = createHash("sha256")
      .update(JSON.stringify({
        from_position: input.from_position,
        summary_text: input.summary_text,
        to_position: input.to_position
      }))
      .digest("hex");

    const result = this.db
      .prepare(
        `
        INSERT OR IGNORE INTO conversation_summaries (
          id, user_id, session_id, source_run_id, from_position, to_position,
          summary_text, summary_hash, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      `
      )
      .run(
        input.id,
        input.user_id,
        input.session_id,
        input.source_run_id ?? null,
        input.from_position,
        input.to_position,
        input.summary_text,
        summaryHash,
        createdAt
      );

    if (result.changes === 1) {
      return this.get({ user_id: input.user_id, summary_id: input.id });
    }

    return this.getByRange({
      user_id: input.user_id,
      session_id: input.session_id,
      from_position: input.from_position,
      to_position: input.to_position
    });
  }

  get(input: { user_id: string; summary_id: string }): ConversationSummaryRecord {
    const summary = mapConversationSummaryRow(
      this.db
        .prepare("SELECT * FROM conversation_summaries WHERE user_id = ? AND id = ?")
        .get(input.user_id, input.summary_id)
    );

    if (!summary) {
      throw new Error(`Conversation summary not found: ${input.summary_id}`);
    }

    return summary;
  }

  latest(input: { user_id: string; session_id: string }): Optional<ConversationSummaryRecord> {
    return mapConversationSummaryRow(
      this.db
        .prepare(
          `
          SELECT * FROM conversation_summaries
          WHERE user_id = ? AND session_id = ?
          ORDER BY to_position DESC, created_at DESC
          LIMIT 1
        `
        )
        .get(input.user_id, input.session_id)
    );
  }

  private getByRange(input: {
    user_id: string;
    session_id: string;
    from_position: number;
    to_position: number;
  }): ConversationSummaryRecord {
    const summary = mapConversationSummaryRow(
      this.db
        .prepare(
          `
          SELECT * FROM conversation_summaries
          WHERE user_id = ? AND session_id = ? AND from_position = ? AND to_position = ?
        `
        )
        .get(input.user_id, input.session_id, input.from_position, input.to_position)
    );

    if (!summary) {
      throw new Error(`Conversation summary conflict could not be resolved: ${input.session_id}`);
    }

    return summary;
  }
}

export class LongTermMemoryRepository {
  constructor(private readonly db: DatabaseSync) {}

  upsert(input: CreateLongTermMemoryInput): LongTermMemoryRecord {
    const now = new Date().toISOString();
    const contentJson = JSON.stringify(input.content ?? { text: input.content_text });
    const memoryHash = createLongTermMemoryHash(input);
    const result = this.db
      .prepare(
        `
        INSERT INTO long_term_memories (
          id, user_id, scope, session_id, datasource_id, kind, content_json, content_text,
          memory_hash, confidence, status, source, source_run_id, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
        ON CONFLICT(user_id, memory_hash) DO UPDATE SET
          content_json = excluded.content_json,
          content_text = excluded.content_text,
          confidence = excluded.confidence,
          status = 'active',
          source = excluded.source,
          source_run_id = excluded.source_run_id,
          updated_at = excluded.updated_at
      `
      )
      .run(
        input.id,
        input.user_id,
        input.scope,
        input.session_id ?? null,
        input.datasource_id ?? null,
        input.kind,
        contentJson,
        input.content_text,
        memoryHash,
        clampConfidence(input.confidence ?? 0.8),
        input.source ?? null,
        input.source_run_id ?? null,
        now,
        now
      );

    if (result.changes < 1) {
      throw new Error(`Unable to upsert long-term memory: ${input.id}`);
    }

    return this.getByHash({ user_id: input.user_id, memory_hash: memoryHash });
  }

  get(input: { user_id: string; memory_id: string }): LongTermMemoryRecord {
    const memory = mapLongTermMemoryRow(
      this.db
        .prepare("SELECT * FROM long_term_memories WHERE user_id = ? AND id = ?")
        .get(input.user_id, input.memory_id)
    );

    if (!memory) {
      throw new Error(`Long-term memory not found: ${input.memory_id}`);
    }

    return memory;
  }

  listRelevant(input: ListRelevantLongTermMemoriesInput): LongTermMemoryRecord[] {
    const limit = Math.max(0, Math.floor(input.limit));
    if (limit === 0) {
      return [];
    }
    const rows = this.db
      .prepare(
        `
        SELECT * FROM long_term_memories
        WHERE user_id = ?
          AND status = 'active'
          AND (
            scope = 'user'
            OR (scope = 'session' AND session_id = ?)
            OR (scope = 'datasource' AND datasource_id = ?)
          )
        ORDER BY updated_at DESC
        LIMIT ?
      `
      )
      .all(input.user_id, input.session_id ?? null, input.datasource_id ?? null, Math.max(limit * 4, limit));
    const queryTerms = tokenizeMemoryText(input.query);
    return rows
      .map(mapRequiredLongTermMemoryRow)
      .map((memory) => ({ memory, score: scoreLongTermMemory(memory, queryTerms, input) }))
      .filter((entry) => entry.score > 0)
      .sort((left, right) => right.score - left.score)
      .slice(0, limit)
      .map((entry) => entry.memory);
  }

  markAccessed(input: { user_id: string; memory_ids: string[] }): void {
    if (input.memory_ids.length === 0) {
      return;
    }
    const now = new Date().toISOString();
    const statement = this.db.prepare(
      "UPDATE long_term_memories SET last_accessed_at = ? WHERE user_id = ? AND id = ?"
    );
    this.db.exec("BEGIN");
    try {
      input.memory_ids.forEach((id) => statement.run(now, input.user_id, id));
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  private getByHash(input: { user_id: string; memory_hash: string }): LongTermMemoryRecord {
    const memory = mapLongTermMemoryRow(
      this.db
        .prepare("SELECT * FROM long_term_memories WHERE user_id = ? AND memory_hash = ?")
        .get(input.user_id, input.memory_hash)
    );

    if (!memory) {
      throw new Error(`Long-term memory conflict could not be resolved: ${input.memory_hash}`);
    }

    return memory;
  }
}

export class ContextPackageSnapshotRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateContextPackageSnapshotInput): ContextPackageSnapshotRecord {
    const createdAt = new Date().toISOString();
    const id = contextPackageSnapshotId(input.package_id, input.revision);

    this.db
      .prepare(
        `
        INSERT INTO context_package_snapshots (
          id, user_id, session_id, run_id, package_id, revision, payload_json, plan_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, id) DO UPDATE SET
          payload_json = excluded.payload_json,
          plan_json = excluded.plan_json
      `
      )
      .run(
        id,
        input.user_id,
        input.session_id,
        input.run_id,
        input.package_id,
        input.revision,
        JSON.stringify(input.payload),
        input.plan === undefined ? null : JSON.stringify(input.plan),
        createdAt
      );

    return this.get({ user_id: input.user_id, id });
  }

  find(input: { user_id: string; id: string }): Optional<ContextPackageSnapshotRecord> {
    return mapContextPackageSnapshotRow(
      this.db
        .prepare("SELECT * FROM context_package_snapshots WHERE user_id = ? AND id = ?")
        .get(input.user_id, input.id)
    );
  }

  findByPackageRevision(input: {
    user_id: string;
    package_id: string;
    revision: number;
  }): Optional<ContextPackageSnapshotRecord> {
    return this.find({
      user_id: input.user_id,
      id: contextPackageSnapshotId(input.package_id, input.revision)
    });
  }

  get(input: { user_id: string; id: string }): ContextPackageSnapshotRecord {
    const snapshot = this.find(input);
    if (!snapshot) {
      throw new Error(`Context package snapshot not found: ${input.id}`);
    }
    return snapshot;
  }

  latestByRun(input: { user_id: string; run_id: string }): Optional<ContextPackageSnapshotRecord> {
    return mapContextPackageSnapshotRow(
      this.db
        .prepare(
          `
          SELECT *
          FROM context_package_snapshots
          WHERE user_id = ? AND run_id = ?
          ORDER BY revision DESC, created_at DESC
          LIMIT 1
        `
        )
        .get(input.user_id, input.run_id)
    );
  }
}

export class ProtocolStateSnapshotRepository {
  constructor(private readonly db: DatabaseSync) {}

  compareAndSet(input: CompareAndSetProtocolStateInput): ProtocolStateSnapshotRecord {
    const state = requireProtocolStateFields(input.state);
    if (state.runId !== input.run_id || state.segmentId !== input.segment_id) {
      throw new Error(`PROTOCOL_STATE_SCOPE_MISMATCH:${input.run_id}:${input.segment_id}`);
    }
    const current = this.find({
      user_id: input.user_id,
      run_id: input.run_id,
      segment_id: input.segment_id
    });
    const currentRevision = current?.revision ?? -1;
    if (currentRevision !== input.expected_revision) {
      throw new Error(
        `PROTOCOL_REVISION_CONFLICT:${input.run_id}:${input.segment_id}:`
        + `${input.expected_revision}:${currentRevision}`
      );
    }
    if (state.revision !== input.expected_revision + 1) {
      throw new Error(
        `PROTOCOL_REVISION_INVALID:${input.run_id}:${input.segment_id}:${state.revision}`
      );
    }
    const contextExists = this.db.prepare(`
      SELECT 1 AS found
      FROM context_package_snapshots
      WHERE user_id = ? AND package_id = ? AND revision = ?
    `).get(input.user_id, state.contextPackageId, state.contextPackageRevision);
    if (!contextExists) {
      throw new Error(
        `PROTOCOL_CONTEXT_REF_NOT_FOUND:${state.contextPackageId}@${state.contextPackageRevision}`
      );
    }
    const now = new Date().toISOString();
    if (!current) {
      this.db.prepare(`
        INSERT INTO protocol_state_snapshots (
          user_id, run_id, segment_id, protocol_id, protocol_version, revision, phase, status,
          context_package_id, context_package_revision, state_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `).run(
        input.user_id,
        input.run_id,
        input.segment_id,
        state.protocolId,
        state.protocolVersion,
        state.revision,
        state.phase,
        state.status,
        state.contextPackageId,
        state.contextPackageRevision,
        JSON.stringify(input.state),
        now,
        now
      );
    } else {
      const result = this.db.prepare(`
        UPDATE protocol_state_snapshots
        SET protocol_id = ?, protocol_version = ?, revision = ?, phase = ?, status = ?,
            context_package_id = ?, context_package_revision = ?, state_json = ?, updated_at = ?
        WHERE user_id = ? AND run_id = ? AND segment_id = ? AND revision = ?
      `).run(
        state.protocolId,
        state.protocolVersion,
        state.revision,
        state.phase,
        state.status,
        state.contextPackageId,
        state.contextPackageRevision,
        JSON.stringify(input.state),
        now,
        input.user_id,
        input.run_id,
        input.segment_id,
        input.expected_revision
      );
      if (result.changes !== 1) {
        throw new Error(
          `PROTOCOL_REVISION_CONFLICT:${input.run_id}:${input.segment_id}:`
          + `${input.expected_revision}:${currentRevision}`
        );
      }
    }
    return this.get({ user_id: input.user_id, run_id: input.run_id, segment_id: input.segment_id });
  }

  compareAndSetWithEvents(
    input: CompareAndSetProtocolStateInput,
    events: unknown[]
  ): ProtocolStateSnapshotRecord {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const state = this.compareAndSet(input);
      this.appendEvents(input.user_id, events);
      this.db.exec("COMMIT");
      return state;
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  transitionSegment(input: TransitionProtocolSegmentInput): {
    current: ProtocolStateSnapshotRecord;
    next: ProtocolStateSnapshotRecord;
  } {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const current = this.compareAndSet({ user_id: input.user_id, ...input.current });
      const next = this.compareAndSet({ user_id: input.user_id, ...input.next });
      this.appendEvents(input.user_id, input.events ?? []);
      this.db.exec("COMMIT");
      return { current, next };
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  find(input: {
    user_id: string;
    run_id: string;
    segment_id: string;
  }): Optional<ProtocolStateSnapshotRecord> {
    return mapProtocolStateSnapshotRow(this.db.prepare(`
      SELECT * FROM protocol_state_snapshots
      WHERE user_id = ? AND run_id = ? AND segment_id = ?
    `).get(input.user_id, input.run_id, input.segment_id));
  }

  get(input: { user_id: string; run_id: string; segment_id: string }): ProtocolStateSnapshotRecord {
    const snapshot = this.find(input);
    if (!snapshot) {
      throw new Error(`Protocol state snapshot not found: ${input.run_id}:${input.segment_id}`);
    }
    return snapshot;
  }

  latestByRun(input: { user_id: string; run_id: string }): Optional<ProtocolStateSnapshotRecord> {
    return mapProtocolStateSnapshotRow(this.db.prepare(`
      SELECT * FROM protocol_state_snapshots
      WHERE user_id = ? AND run_id = ?
      ORDER BY rowid DESC
      LIMIT 1
    `).get(input.user_id, input.run_id));
  }

  acknowledgeEvent(input: { user_id: string; event_id: string }): void {
    this.db.prepare(`
      UPDATE protocol_event_journal
      SET published_at = ?
      WHERE user_id = ? AND event_id = ?
    `).run(new Date().toISOString(), input.user_id, input.event_id);
  }

  pendingEvents(input: { user_id: string; run_id: string }): unknown[] {
    return this.db.prepare(`
      SELECT event_json FROM protocol_event_journal
      WHERE user_id = ? AND run_id = ? AND published_at IS NULL
      ORDER BY created_at ASC, rowid ASC
    `).all(input.user_id, input.run_id).map((row) => {
      if (!isRecord(row) || typeof row.event_json !== "string") {
        throw new Error(`PROTOCOL_EVENT_JOURNAL_INVALID:${input.run_id}`);
      }
      return JSON.parse(row.event_json) as unknown;
    });
  }

  private appendEvents(userId: string, events: unknown[]): void {
    const statement = this.db.prepare(`
      INSERT OR IGNORE INTO protocol_event_journal (
        user_id, run_id, segment_id, event_id, revision, event_type, event_json, created_at, published_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
    `);
    const now = new Date().toISOString();
    for (const event of events) {
      const fields = requireProtocolEventFields(event);
      statement.run(
        userId,
        fields.runId,
        fields.segmentId,
        fields.eventId,
        fields.revision,
        fields.type,
        JSON.stringify(event),
        now
      );
    }
  }
}

export class CheckpointRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateCheckpointInput): CheckpointRecord {
    const createdAt = new Date().toISOString();

    this.db
      .prepare(
        `
        INSERT INTO checkpoints (
          id, user_id, session_id, run_id, branch_id, event_seq, context_package_id,
          context_package_revision, context_plan_id, kind, status, label, parent_checkpoint_id,
          step_number, step_id, tool_call_id, message_position, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, id) DO NOTHING
      `
      )
      .run(
        input.id,
        input.user_id,
        input.session_id,
        input.run_id,
        input.branch_id ?? "main",
        input.event_seq,
        input.context_package_id,
        input.context_package_revision,
        input.context_plan_id ?? null,
        input.kind,
        input.status,
        input.label,
        input.parent_checkpoint_id ?? null,
        input.step_number ?? null,
        input.step_id ?? null,
        input.tool_call_id ?? null,
        input.message_position ?? null,
        createdAt
      );

    return this.get({ user_id: input.user_id, checkpoint_id: input.id });
  }

  find(input: { user_id: string; checkpoint_id: string }): Optional<CheckpointRecord> {
    return mapCheckpointRow(
      this.db
        .prepare("SELECT * FROM checkpoints WHERE user_id = ? AND id = ?")
        .get(input.user_id, input.checkpoint_id)
    );
  }

  get(input: { user_id: string; checkpoint_id: string }): CheckpointRecord {
    const checkpoint = this.find(input);
    if (!checkpoint) {
      throw new Error(`Checkpoint not found: ${input.checkpoint_id}`);
    }
    return checkpoint;
  }

  latestByRun(input: { user_id: string; run_id: string }): Optional<CheckpointRecord> {
    return mapCheckpointRow(
      this.db
        .prepare(
          `
          SELECT *
          FROM checkpoints
          WHERE user_id = ? AND run_id = ?
          ORDER BY event_seq DESC
          LIMIT 1
        `
        )
        .get(input.user_id, input.run_id)
    );
  }

  listBySession(input: { user_id: string; session_id: string; limit?: number }): CheckpointRecord[] {
    const limit = input.limit ?? 200;
    return this.db
      .prepare(
        `
        SELECT *
        FROM checkpoints
        WHERE user_id = ? AND session_id = ?
        ORDER BY created_at ASC, event_seq ASC
        LIMIT ?
      `
      )
      .all(input.user_id, input.session_id, limit)
      .map(mapRequiredCheckpointRow);
  }

  listByRun(input: { user_id: string; run_id: string }): CheckpointRecord[] {
    return this.db
      .prepare(
        "SELECT * FROM checkpoints WHERE user_id = ? AND run_id = ? ORDER BY event_seq ASC, created_at ASC"
      )
      .all(input.user_id, input.run_id)
      .map(mapRequiredCheckpointRow);
  }
}

export class TraceSectionRepository {
  constructor(private readonly db: DatabaseSync) {}

  upsert(input: UpsertTraceSectionInput): TraceSectionRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO trace_sections (
        id, user_id, session_id, run_id, branch_id, phase_key, start_event_seq,
        end_event_seq, status, title, summary, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(user_id, id) DO UPDATE SET
        phase_key = excluded.phase_key,
        end_event_seq = excluded.end_event_seq,
        status = excluded.status,
        title = excluded.title,
        summary = excluded.summary,
        updated_at = excluded.updated_at
    `).run(
      input.id,
      input.user_id,
      input.session_id,
      input.run_id,
      input.branch_id ?? "main",
      input.phase_key,
      input.start_event_seq,
      input.end_event_seq,
      input.status,
      input.title,
      input.summary,
      now,
      now
    );
    return this.get({ user_id: input.user_id, trace_section_id: input.id });
  }

  findOpenByRun(input: { user_id: string; run_id: string }): Optional<TraceSectionRecord> {
    return mapTraceSectionRow(this.db.prepare(`
      SELECT * FROM trace_sections
      WHERE user_id = ? AND run_id = ? AND status = 'in-progress'
      ORDER BY updated_at DESC
      LIMIT 1
    `).get(input.user_id, input.run_id));
  }

  get(input: { user_id: string; trace_section_id: string }): TraceSectionRecord {
    const record = mapTraceSectionRow(this.db.prepare(
      "SELECT * FROM trace_sections WHERE user_id = ? AND id = ?"
    ).get(input.user_id, input.trace_section_id));
    if (!record) {
      throw new Error(`Trace section not found: ${input.trace_section_id}`);
    }
    return record;
  }

  listBySessions(input: { user_id: string; session_ids: string[] }): TraceSectionRecord[] {
    if (input.session_ids.length === 0) {
      return [];
    }
    const placeholders = input.session_ids.map(() => "?").join(", ");
    return this.db.prepare(`
      SELECT * FROM trace_sections
      WHERE user_id = ? AND session_id IN (${placeholders})
      ORDER BY created_at ASC, start_event_seq ASC
    `).all(input.user_id, ...input.session_ids).map(mapRequiredTraceSectionRow);
  }

  listByRun(input: { user_id: string; run_id: string }): TraceSectionRecord[] {
    return this.db.prepare(`
      SELECT * FROM trace_sections
      WHERE user_id = ? AND run_id = ?
      ORDER BY start_event_seq ASC, updated_at ASC
    `).all(input.user_id, input.run_id).map(mapRequiredTraceSectionRow);
  }
}

export class ArtifactRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateArtifactInput): ArtifactRecord {
    const createdAt = new Date().toISOString();

    this.db
      .prepare(
        `
        INSERT INTO artifacts (
          id, user_id, session_id, run_id, type, name, mime_type,
          storage_path, file_asset_ref_id, preview_json, metadata_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `
      )
      .run(
        input.id,
        input.user_id,
        input.session_id,
        input.run_id,
        input.type,
        input.name,
        input.mime_type ?? null,
        input.storage_path ?? null,
        input.file_asset_ref_id ?? null,
        input.preview_json === undefined ? null : JSON.stringify(input.preview_json),
        input.metadata_json === undefined ? null : JSON.stringify(input.metadata_json),
        createdAt
      );

    return this.get({ user_id: input.user_id, artifact_id: input.id });
  }

  get(input: { user_id: string; artifact_id: string }): ArtifactRecord {
    const artifact = mapArtifactRow(
      this.db.prepare("SELECT * FROM artifacts WHERE user_id = ? AND id = ?").get(input.user_id, input.artifact_id)
    );

    if (!artifact) {
      throw new Error(`Artifact not found: ${input.artifact_id}`);
    }

    return artifact;
  }

  findBySessionLogicalKey(
    input: { user_id: string; session_id: string; logical_key: string }
  ): Optional<ArtifactRecord> {
    const records = this.listBySession({
      user_id: input.user_id,
      session_id: input.session_id
    });
    return records.find((record) => {
      if (!record.metadata_json) {
        return false;
      }
      try {
        const metadata = JSON.parse(record.metadata_json) as unknown;
        return isRecord(metadata) && metadata.logical_key === input.logical_key;
      } catch {
        return false;
      }
    });
  }

  updateFileAssetRef(input: {
    user_id: string;
    artifact_id: string;
    run_id: string;
    type: ArtifactType;
    name: string;
    mime_type?: string;
    file_asset_ref_id: string;
    preview_json?: unknown;
    metadata_json?: unknown;
  }): ArtifactRecord {
    this.db.prepare(`
      UPDATE artifacts
      SET run_id = ?,
        type = ?,
        name = ?,
        mime_type = ?,
        file_asset_ref_id = ?,
        preview_json = ?,
        metadata_json = ?
      WHERE user_id = ? AND id = ?
    `).run(
      input.run_id,
      input.type,
      input.name,
      input.mime_type ?? null,
      input.file_asset_ref_id,
      input.preview_json === undefined ? null : JSON.stringify(input.preview_json),
      input.metadata_json === undefined ? null : JSON.stringify(input.metadata_json),
      input.user_id,
      input.artifact_id
    );
    return this.get({ user_id: input.user_id, artifact_id: input.artifact_id });
  }

  listByRun(input: { user_id: string; run_id: string }): ArtifactRecord[] {
    return this.db
      .prepare("SELECT * FROM artifacts WHERE user_id = ? AND run_id = ? ORDER BY created_at ASC")
      .all(input.user_id, input.run_id)
      .map(mapRequiredArtifactRow);
  }

  /** List artifacts for a session (R-023 session-restore). Stable ASC by created_at. */
  listBySession(input: { user_id: string; session_id: string }): ArtifactRecord[] {
    return this.db
      .prepare("SELECT * FROM artifacts WHERE user_id = ? AND session_id = ? ORDER BY created_at ASC")
      .all(input.user_id, input.session_id)
      .map(mapRequiredArtifactRow);
  }
}

export class ArtifactVersionRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateArtifactVersionInput): ArtifactVersionRecord {
    const createdAt = new Date().toISOString();
    const row = this.db.prepare(`
      SELECT COALESCE(MAX(version), 0) + 1 AS next_version
      FROM artifact_versions
      WHERE user_id = ? AND artifact_id = ?
    `).get(input.user_id, input.artifact_id);
    if (!isRecord(row) || typeof row.next_version !== "number") {
      throw new Error(`Unable to allocate artifact version for artifact: ${input.artifact_id}`);
    }
    this.db.prepare(`
      INSERT INTO artifact_versions (
        id, user_id, artifact_id, version, file_asset_ref_id, preview_json, content_hash, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    `).run(
      input.id,
      input.user_id,
      input.artifact_id,
      row.next_version,
      input.file_asset_ref_id ?? null,
      input.preview_json === undefined ? null : JSON.stringify(input.preview_json),
      input.content_hash ?? null,
      createdAt
    );
    return this.get({ user_id: input.user_id, version_id: input.id });
  }

  get(input: { user_id: string; version_id: string }): ArtifactVersionRecord {
    const version = mapArtifactVersionRow(
      this.db.prepare("SELECT * FROM artifact_versions WHERE user_id = ? AND id = ?")
        .get(input.user_id, input.version_id)
    );
    if (!version) {
      throw new Error(`Artifact version not found: ${input.version_id}`);
    }
    return version;
  }

  listByArtifact(input: { user_id: string; artifact_id: string }): ArtifactVersionRecord[] {
    return this.db.prepare(`
      SELECT *
      FROM artifact_versions
      WHERE user_id = ? AND artifact_id = ?
      ORDER BY version ASC
    `).all(input.user_id, input.artifact_id).map(mapRequiredArtifactVersionRow);
  }
}

export class FileAssetRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateFileAssetInput): FileAssetRecord {
    const createdAt = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO file_assets (id, sha256, size_bytes, storage_path, detected_mime_type, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
      ON CONFLICT(sha256) DO NOTHING
    `).run(
      input.id,
      input.sha256,
      input.size_bytes,
      input.storage_path,
      input.detected_mime_type ?? null,
      createdAt
    );
    return this.getBySha256(input.sha256);
  }

  get(input: { id: string }): FileAssetRecord {
    const asset = mapFileAssetRow(this.db.prepare("SELECT * FROM file_assets WHERE id = ?").get(input.id));
    if (!asset) {
      throw new Error(`FILE_ASSET_NOT_FOUND:${input.id}`);
    }
    return asset;
  }

  getBySha256(sha256: string): FileAssetRecord {
    const asset = mapFileAssetRow(this.db.prepare("SELECT * FROM file_assets WHERE sha256 = ?").get(sha256));
    if (!asset) {
      throw new Error(`FILE_ASSET_NOT_FOUND_BY_SHA256:${sha256}`);
    }
    return asset;
  }

  findBySha256(sha256: string): Optional<FileAssetRecord> {
    return mapFileAssetRow(this.db.prepare("SELECT * FROM file_assets WHERE sha256 = ?").get(sha256));
  }

  refCount(input: { id: string }): number {
    const row = this.db.prepare(`
      SELECT COUNT(*) AS count FROM file_asset_refs WHERE file_asset_id = ? AND status != 'deleted'
    `).get(input.id);
    return isRecord(row) && typeof row.count === "number" ? row.count : 0;
  }

  /**
   * List assets with zero non-deleted refs — orphaned content that can be GC'd.
   * A reassignAsset leaves the previous asset orphaned; this surfaces them so the
   * file asset service can delete their on-disk content and records.
   */
  listOrphans(): FileAssetRecord[] {
    const rows = this.db.prepare(`
      SELECT a.* FROM file_assets a
      WHERE NOT EXISTS (
        SELECT 1 FROM file_asset_refs r
        WHERE r.file_asset_id = a.id AND r.status != 'deleted'
      )
    `).all();
    return rows.map(mapFileAssetRow).filter((asset): asset is FileAssetRecord => Boolean(asset));
  }

  /** Hard-delete an asset record (use only after confirming it is orphaned). */
  hardDelete(input: { id: string }): void {
    this.db.prepare("DELETE FROM file_assets WHERE id = ?").run(input.id);
  }
}

export class FileAssetRefRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateFileAssetRefInput): FileAssetRefRecord {
    const createdAt = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO file_asset_refs (
        id, file_asset_id, user_id, workspace_id, filename, declared_mime_type, source, status,
        session_id, run_id, metadata_json, created_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)
    `).run(
      input.id,
      input.file_asset_id,
      input.user_id,
      input.workspace_id,
      input.filename,
      input.declared_mime_type ?? null,
      input.source,
      input.session_id ?? null,
      input.run_id ?? null,
      input.metadata_json === undefined ? null : JSON.stringify(input.metadata_json),
      createdAt
    );
    return this.get({ user_id: input.user_id, workspace_id: input.workspace_id, id: input.id });
  }

  get(input: { user_id: string; workspace_id: string; id: string }): FileAssetRefRecord {
    const ref = mapFileAssetRefRow(this.db.prepare(`
      SELECT * FROM file_asset_refs WHERE user_id = ? AND workspace_id = ? AND id = ?
    `).get(input.user_id, input.workspace_id, input.id));
    if (!ref || ref.status === "deleted") {
      throw new Error(`FILE_ASSET_REF_NOT_FOUND:${input.id}`);
    }
    return ref;
  }

  list(input: {
    user_id: string;
    workspace_id: string;
    limit?: number;
    source?: FileAssetRefSource;
    /**
     * Session filter. Pass a session_id to match refs scoped to that session;
     * pass `null` to match only cross-session (workspace-scoped) refs where
     * session_id IS NULL; omit to return refs regardless of session.
     */
    session_id?: string | null;
    /** When true, match only refs that HAVE a session_id (scope=session w/o a id). */
    has_session?: boolean;
  }): FileAssetRefRecord[] {
    const limit = Math.max(1, Math.min(input.limit ?? 100, 500));
    const where: string[] = ["user_id = ?", "workspace_id = ?", "status != 'deleted'"];
    const params: (string | number)[] = [input.user_id, input.workspace_id];
    if (input.source) {
      where.push("source = ?");
      params.push(input.source);
    }
    if (input.session_id === null) {
      where.push("session_id IS NULL");
    } else if (input.session_id) {
      where.push("session_id = ?");
      params.push(input.session_id);
    } else if (input.has_session) {
      where.push("session_id IS NOT NULL");
    }
    params.push(limit);
    const rows = this.db.prepare(`
      SELECT * FROM file_asset_refs
      WHERE ${where.join(" AND ")}
      ORDER BY created_at DESC
      LIMIT ?
    `).all(...params);
    return rows.map(mapRequiredFileAssetRefRow);
  }

  findActiveByFilename(input: {
    user_id: string;
    workspace_id: string;
    filename: string;
    source?: FileAssetRefSource;
    session_id?: string | null;
    has_session?: boolean;
  }): FileAssetRefRecord | undefined {
    const where: string[] = ["user_id = ?", "workspace_id = ?", "filename = ?", "status != 'deleted'"];
    const params: string[] = [input.user_id, input.workspace_id, input.filename];
    if (input.source) {
      where.push("source = ?");
      params.push(input.source);
    }
    if (input.session_id === null) {
      where.push("session_id IS NULL");
    } else if (input.session_id) {
      where.push("session_id = ?");
      params.push(input.session_id);
    } else if (input.has_session) {
      where.push("session_id IS NOT NULL");
    }
    const row = this.db.prepare(`
      SELECT * FROM file_asset_refs
      WHERE ${where.join(" AND ")}
      ORDER BY created_at DESC
      LIMIT 1
    `).get(...params);
    return mapFileAssetRefRow(row) ?? undefined;
  }

  softDelete(input: { user_id: string; workspace_id: string; id: string }): FileAssetRefRecord {
    this.db.prepare(`
      UPDATE file_asset_refs SET status = 'deleted'
      WHERE user_id = ? AND workspace_id = ? AND id = ?
    `).run(input.user_id, input.workspace_id, input.id);
    const ref = mapFileAssetRefRow(this.db.prepare(`
      SELECT * FROM file_asset_refs WHERE user_id = ? AND workspace_id = ? AND id = ?
    `).get(input.user_id, input.workspace_id, input.id));
    if (!ref) {
      throw new Error(`FILE_ASSET_REF_NOT_FOUND:${input.id}`);
    }
    return ref;
  }

  /**
   * Reassign a ref to point at a different asset (by asset id). Used when a workspace
   * file's content changed (e.g. agent edit_file): the ref id stays stable (so external
   * file_id references remain valid) but its file_asset_id moves to the new content's
   * asset. The previously-pointed asset becomes an orphan candidate for GC.
   */
  reassignAsset(
    input: { user_id: string; workspace_id: string; id: string; file_asset_id: string }
  ): FileAssetRefRecord {
    this.db.prepare(`
      UPDATE file_asset_refs SET file_asset_id = ?
      WHERE user_id = ? AND workspace_id = ? AND id = ? AND status != 'deleted'
    `).run(input.file_asset_id, input.user_id, input.workspace_id, input.id);
    const ref = mapFileAssetRefRow(this.db.prepare(`
      SELECT * FROM file_asset_refs WHERE user_id = ? AND workspace_id = ? AND id = ?
    `).get(input.user_id, input.workspace_id, input.id));
    if (!ref || ref.status === "deleted") {
      throw new Error(`FILE_ASSET_REF_NOT_FOUND:${input.id}`);
    }
    return ref;
  }
}

export class SqlAuditLogRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateSqlAuditLogInput): SqlAuditLogRecord {
    const createdAt = new Date().toISOString();

    this.db
      .prepare(
        `
        INSERT INTO sql_audit_logs (
          id, user_id, run_id, datasource_id, sql_text, status,
          blocked_reason, row_count, elapsed_ms, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `
      )
      .run(
        input.id,
        input.user_id,
        input.run_id ?? null,
        input.datasource_id,
        input.sql_text,
        input.status,
        input.blocked_reason ?? null,
        input.row_count ?? null,
        input.elapsed_ms ?? null,
        createdAt
      );

    return this.get({ user_id: input.user_id, audit_log_id: input.id });
  }

  get(input: { user_id: string; audit_log_id: string }): SqlAuditLogRecord {
    const log = mapSqlAuditLogRow(
      this.db
        .prepare("SELECT * FROM sql_audit_logs WHERE user_id = ? AND id = ?")
        .get(input.user_id, input.audit_log_id)
    );

    if (!log) {
      throw new Error(`SQL audit log not found: ${input.audit_log_id}`);
    }

    return log;
  }

  listByRun(input: { user_id: string; run_id: string }): SqlAuditLogRecord[] {
    return this.db
      .prepare("SELECT * FROM sql_audit_logs WHERE user_id = ? AND run_id = ? ORDER BY created_at ASC")
      .all(input.user_id, input.run_id)
      .map(mapRequiredSqlAuditLogRow);
  }

  listByDataSource(input: { user_id: string; datasource_id: string }): SqlAuditLogRecord[] {
    return this.db
      .prepare("SELECT * FROM sql_audit_logs WHERE user_id = ? AND datasource_id = ? ORDER BY created_at ASC")
      .all(input.user_id, input.datasource_id)
      .map(mapRequiredSqlAuditLogRow);
  }
}

export class QueryHistoryRepository {
  constructor(private readonly db: DatabaseSync) {}

  create(input: CreateQueryHistoryInput): QueryHistoryRecord {
    const now = new Date().toISOString();
    const id = createHash("sha256")
      .update(JSON.stringify({
        user_id: input.user_id,
        workspace_id: input.workspace_id,
        session_id: input.session_id,
        datasource_id: input.datasource_id,
        sql_text: input.sql_text
      }))
      .digest("hex");
    this.db.prepare(`
      INSERT INTO query_history (
        id, user_id, workspace_id, session_id, run_id, datasource_id, sql_text,
        row_count, elapsed_ms, favorite, created_at, updated_at
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
      ON CONFLICT(user_id, workspace_id, id) DO UPDATE SET
        run_id = excluded.run_id,
        row_count = excluded.row_count,
        elapsed_ms = excluded.elapsed_ms,
        updated_at = excluded.updated_at
    `).run(
      id,
      input.user_id,
      input.workspace_id,
      input.session_id,
      input.run_id ?? null,
      input.datasource_id,
      input.sql_text,
      input.row_count,
      input.elapsed_ms,
      now,
      now
    );
    return this.get({ user_id: input.user_id, workspace_id: input.workspace_id, id });
  }

  get(input: { id: string; user_id: string; workspace_id: string }): QueryHistoryRecord {
    const record = mapQueryHistoryRow(this.db.prepare(`
      SELECT * FROM query_history WHERE user_id = ? AND workspace_id = ? AND id = ?
    `).get(input.user_id, input.workspace_id, input.id));
    if (!record) {
      throw new Error(`QUERY_HISTORY_NOT_FOUND:${input.id}`);
    }
    return record;
  }

  list(input: {
    datasource_id?: string;
    favorite?: boolean;
    limit?: number;
    session_id?: string;
    user_id: string;
    workspace_id: string;
  }): QueryHistoryRecord[] {
    const limit = Math.max(1, Math.min(input.limit ?? 50, 200));
    const where = ["user_id = ?", "workspace_id = ?"];
    const params: Array<string | number> = [input.user_id, input.workspace_id];
    if (input.session_id) {
      where.push("session_id = ?");
      params.push(input.session_id);
    }
    if (input.datasource_id) {
      where.push("datasource_id = ?");
      params.push(input.datasource_id);
    }
    if (input.favorite !== undefined) {
      where.push("favorite = ?");
      params.push(input.favorite ? 1 : 0);
    }
    params.push(limit);
    return this.db.prepare(`
      SELECT * FROM query_history
      WHERE ${where.join(" AND ")}
      ORDER BY updated_at DESC, created_at DESC
      LIMIT ?
    `).all(...params).map(mapRequiredQueryHistoryRow);
  }

  setFavorite(input: { favorite: boolean; id: string; user_id: string; workspace_id: string }): QueryHistoryRecord {
    const now = new Date().toISOString();
    this.db.prepare(`
      UPDATE query_history SET favorite = ?, updated_at = ?
      WHERE user_id = ? AND workspace_id = ? AND id = ?
    `).run(input.favorite ? 1 : 0, now, input.user_id, input.workspace_id, input.id);
    return this.get(input);
  }
}

export class RunEventWriter {
  constructor(private readonly repository: RunEventRepository) {}

  write(input: WriteRunEventInput): RunEventEnvelope {
    const record = this.repository.append(input);

    return runEventRecordToEnvelope(record);
  }

  replay(input: { user_id: string; run_id: string }): RunEventEnvelope[] {
    return this.repository.listByRun(input).map(runEventRecordToEnvelope);
  }
}

export const createMetadataStore = (options: MetadataStoreOptions = {}): MetadataStore => {
  const databasePath = resolve(options.database_path ?? "storage/metadata/workbench.sqlite");
  mkdirSync(dirname(databasePath), { recursive: true });

  const db = new DatabaseSync(databasePath);
  db.exec("PRAGMA journal_mode = WAL");
  db.exec("PRAGMA foreign_keys = ON");
  runMigrations(db);

  const store = new MetadataStore(db, options.secret_master_key);
  store.users.upsertDevUser(options.dev_user ?? DEFAULT_DEV_USER);

  return store;
};

export const runEventRecordToEnvelope = (record: RunEventRecord): RunEventEnvelope => ({
  type: record.event_type,
  run_id: record.run_id,
  session_id: record.session_id,
  seq: record.seq,
  ts: record.created_at,
  event: JSON.parse(record.payload_json) as BaseEvent
});

export const artifactRecordToSummary = (record: ArtifactRecord): ArtifactSummary => {
  const origin = artifactOriginFromMetadata(record.metadata_json);
  return {
    id: record.id,
    type: record.type,
    name: record.name,
    ...(record.preview_json ? { preview_json: JSON.parse(record.preview_json) as unknown } : {}),
    ...(record.run_id ? { run_id: record.run_id } : {}),
    ...(origin.tool_call_id ? { tool_call_id: origin.tool_call_id } : {}),
    ...(origin.step_id ? { step_id: origin.step_id } : {})
  };
};

/**
 * Extract authoritative origin handles (`tool_call_id` / `step_id`) that producers
 * persist in the artifact's `metadata_json` (R-018). Returns empty handles when the
 * column is absent or malformed.
 */
const artifactOriginFromMetadata = (
  metadataJson: string | undefined
): { step_id?: string; tool_call_id?: string } => {
  if (!metadataJson) {
    return {};
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(metadataJson);
  } catch {
    return {};
  }
  if (!isRecord(parsed)) {
    return {};
  }
  const toolCallId = typeof parsed.tool_call_id === "string" ? parsed.tool_call_id : undefined;
  const stepId = typeof parsed.step_id === "string" ? parsed.step_id : undefined;
  return {
    ...(toolCallId ? { tool_call_id: toolCallId } : {}),
    ...(stepId ? { step_id: stepId } : {})
  };
};

const runMigrations = (db: DatabaseSync): void => {
  initializeSchemaMigrationTable(db);
  db.exec(`
    CREATE TABLE IF NOT EXISTS users (
      id TEXT PRIMARY KEY,
      email TEXT UNIQUE,
      display_name TEXT,
      dev_token TEXT UNIQUE,
      email_verified_at TEXT,
      disabled_at TEXT,
      password_updated_at TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      title TEXT,
      title_source TEXT,
      last_message_at TEXT,
      selected_datasource_id TEXT,
      selected_collection_id TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (user_id, id),
      FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

    CREATE TABLE IF NOT EXISTS session_branches (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      child_session_id TEXT NOT NULL,
      parent_session_id TEXT NOT NULL,
      root_session_id TEXT NOT NULL,
      fork_run_id TEXT NOT NULL,
      fork_checkpoint_id TEXT,
      fork_message_end_position INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, id),
      UNIQUE (user_id, child_session_id),
      FOREIGN KEY (user_id, child_session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, parent_session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, root_session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, fork_run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_session_branches_parent_fork
      ON session_branches(user_id, parent_session_id, fork_run_id, created_at);

    CREATE TABLE IF NOT EXISTS data_sources (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      name TEXT NOT NULL,
      type TEXT NOT NULL,
      config_json TEXT NOT NULL,
      credential_ref TEXT,
      description TEXT,
      status TEXT NOT NULL,
      last_test_at TEXT,
      revision INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (user_id, id),
      FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_data_sources_user ON data_sources(user_id);

    CREATE TABLE IF NOT EXISTS runs (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      parent_run_id TEXT,
      request_fingerprint TEXT,
      status TEXT NOT NULL,
      user_input TEXT NOT NULL,
      model_provider TEXT,
      model_name TEXT,
      datasource_id TEXT,
      collection_id TEXT,
      started_at TEXT NOT NULL,
      finished_at TEXT,
      error_message TEXT,
      PRIMARY KEY (user_id, id),
      FOREIGN KEY (user_id) REFERENCES users(id),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, parent_run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_runs_user_session ON runs(user_id, session_id);

    CREATE TABLE IF NOT EXISTS run_events (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      seq INTEGER NOT NULL,
      event_type TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, run_id, seq),
      FOREIGN KEY (user_id) REFERENCES users(id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_run_events_user_run ON run_events(user_id, run_id);

    CREATE TABLE IF NOT EXISTS conversation_messages (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      role TEXT NOT NULL,
      source TEXT NOT NULL,
      message_id TEXT,
      content_json TEXT NOT NULL,
      content_text TEXT NOT NULL,
      content_hash TEXT NOT NULL,
      position INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, id),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_conversation_messages_user_session
      ON conversation_messages(user_id, session_id, position);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_messages_message_id
      ON conversation_messages(user_id, session_id, message_id)
      WHERE message_id IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_messages_run_hash
      ON conversation_messages(user_id, session_id, run_id, role, content_hash);

    CREATE TABLE IF NOT EXISTS conversation_summaries (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      source_run_id TEXT,
      from_position INTEGER NOT NULL,
      to_position INTEGER NOT NULL,
      summary_text TEXT NOT NULL,
      summary_hash TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, id),
      UNIQUE (user_id, session_id, from_position, to_position),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, source_run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_conversation_summaries_user_session
      ON conversation_summaries(user_id, session_id, to_position);

    CREATE TABLE IF NOT EXISTS long_term_memories (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      scope TEXT NOT NULL,
      session_id TEXT,
      datasource_id TEXT,
      kind TEXT NOT NULL,
      content_json TEXT NOT NULL,
      content_text TEXT NOT NULL,
      memory_hash TEXT NOT NULL,
      confidence REAL NOT NULL,
      status TEXT NOT NULL,
      source TEXT,
      source_run_id TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      last_accessed_at TEXT,
      PRIMARY KEY (user_id, id),
      UNIQUE (user_id, memory_hash),
      FOREIGN KEY (user_id) REFERENCES users(id),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, source_run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_long_term_memories_user_scope
      ON long_term_memories(user_id, scope, status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_long_term_memories_user_session
      ON long_term_memories(user_id, session_id, status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_long_term_memories_user_datasource
      ON long_term_memories(user_id, datasource_id, status, updated_at);

    CREATE TABLE IF NOT EXISTS context_package_snapshots (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      package_id TEXT NOT NULL,
      revision INTEGER NOT NULL,
      payload_json TEXT NOT NULL,
      plan_json TEXT,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, id),
      UNIQUE (user_id, package_id, revision),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_context_package_snapshots_user_run
      ON context_package_snapshots(user_id, run_id, revision);

    CREATE TABLE IF NOT EXISTS protocol_state_snapshots (
      user_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      segment_id TEXT NOT NULL,
      protocol_id TEXT NOT NULL,
      protocol_version TEXT NOT NULL,
      revision INTEGER NOT NULL,
      phase TEXT NOT NULL,
      status TEXT NOT NULL,
      context_package_id TEXT NOT NULL,
      context_package_revision INTEGER NOT NULL,
      state_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (user_id, run_id, segment_id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id),
      FOREIGN KEY (user_id, context_package_id, context_package_revision)
        REFERENCES context_package_snapshots(user_id, package_id, revision)
    );
    CREATE INDEX IF NOT EXISTS idx_protocol_state_snapshots_user_run
      ON protocol_state_snapshots(user_id, run_id, updated_at);

    CREATE TABLE IF NOT EXISTS checkpoints (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      branch_id TEXT NOT NULL,
      event_seq INTEGER NOT NULL,
      context_package_id TEXT NOT NULL,
      context_package_revision INTEGER NOT NULL,
      context_plan_id TEXT,
      kind TEXT NOT NULL,
      status TEXT NOT NULL,
      label TEXT NOT NULL,
      parent_checkpoint_id TEXT,
      step_number INTEGER,
      step_id TEXT,
      tool_call_id TEXT,
      message_position INTEGER,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, id),
      UNIQUE (user_id, run_id, event_seq, kind),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id),
      FOREIGN KEY (user_id, context_package_id) REFERENCES context_package_snapshots(user_id, id),
      FOREIGN KEY (user_id, parent_checkpoint_id) REFERENCES checkpoints(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_checkpoints_user_session
      ON checkpoints(user_id, session_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_checkpoints_user_run
      ON checkpoints(user_id, run_id, event_seq);

    CREATE TABLE IF NOT EXISTS trace_sections (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      branch_id TEXT NOT NULL,
      phase_key TEXT,
      start_event_seq INTEGER NOT NULL,
      end_event_seq INTEGER NOT NULL,
      status TEXT NOT NULL,
      title TEXT NOT NULL,
      summary TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (user_id, id),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_trace_sections_user_session
      ON trace_sections(user_id, session_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_trace_sections_user_run
      ON trace_sections(user_id, run_id, start_event_seq, end_event_seq);

    CREATE TABLE IF NOT EXISTS file_assets (
      id TEXT PRIMARY KEY,
      sha256 TEXT NOT NULL UNIQUE,
      size_bytes INTEGER NOT NULL,
      storage_path TEXT NOT NULL,
      detected_mime_type TEXT,
      created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_file_assets_sha256 ON file_assets(sha256);

    CREATE TABLE IF NOT EXISTS file_asset_refs (
      id TEXT PRIMARY KEY,
      file_asset_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      workspace_id TEXT NOT NULL,
      filename TEXT NOT NULL,
      declared_mime_type TEXT,
      source TEXT NOT NULL,
      status TEXT NOT NULL,
      session_id TEXT,
      run_id TEXT,
      metadata_json TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY (file_asset_id) REFERENCES file_assets(id),
      FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_file_asset_refs_scope
      ON file_asset_refs(user_id, workspace_id, status, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_file_asset_refs_filename_scope
      ON file_asset_refs(user_id, workspace_id, filename, source, session_id, status, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_file_asset_refs_asset
      ON file_asset_refs(file_asset_id, status);

    CREATE TABLE IF NOT EXISTS artifacts (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      type TEXT NOT NULL,
      name TEXT NOT NULL,
      mime_type TEXT,
      storage_path TEXT,
      file_asset_ref_id TEXT,
      preview_json TEXT,
      metadata_json TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY (user_id) REFERENCES users(id),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id),
      FOREIGN KEY (file_asset_ref_id) REFERENCES file_asset_refs(id)
    );
    CREATE INDEX IF NOT EXISTS idx_artifacts_user_run ON artifacts(user_id, run_id);
    CREATE INDEX IF NOT EXISTS idx_artifacts_user_session ON artifacts(user_id, session_id, created_at);

    CREATE TABLE IF NOT EXISTS sql_audit_logs (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      run_id TEXT,
      datasource_id TEXT NOT NULL,
      sql_text TEXT NOT NULL,
      status TEXT NOT NULL,
      blocked_reason TEXT,
      row_count INTEGER,
      elapsed_ms INTEGER,
      created_at TEXT NOT NULL,
      FOREIGN KEY (user_id) REFERENCES users(id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id),
      FOREIGN KEY (user_id, datasource_id) REFERENCES data_sources(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_sql_audit_logs_user_run ON sql_audit_logs(user_id, run_id);
    CREATE INDEX IF NOT EXISTS idx_sql_audit_logs_user_datasource ON sql_audit_logs(user_id, datasource_id);

    CREATE TABLE IF NOT EXISTS query_history (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      workspace_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      run_id TEXT,
      datasource_id TEXT NOT NULL,
      sql_text TEXT NOT NULL,
      row_count INTEGER NOT NULL,
      elapsed_ms INTEGER NOT NULL,
      favorite INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (user_id, workspace_id, id),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id),
      FOREIGN KEY (user_id, datasource_id) REFERENCES data_sources(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_query_history_user_workspace
      ON query_history(user_id, workspace_id, updated_at);
    CREATE INDEX IF NOT EXISTS idx_query_history_user_session
      ON query_history(user_id, workspace_id, session_id, updated_at);
    CREATE INDEX IF NOT EXISTS idx_query_history_user_datasource
      ON query_history(user_id, workspace_id, datasource_id, updated_at);

    CREATE TABLE IF NOT EXISTS interactions (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      tool_call_id TEXT NOT NULL,
      tool_name TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      status TEXT NOT NULL,
      resume_fingerprint TEXT,
      response_json TEXT,
      created_at TEXT NOT NULL,
      resolved_at TEXT,
      PRIMARY KEY (user_id, id),
      UNIQUE (user_id, run_id, tool_call_id),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_interactions_user_run ON interactions(user_id, run_id);
  `);
  recordSchemaMigration(db, "0001_core_schema", "Create core metadata tables");

  runSchemaMigration(db, "0002_data_source_revision", "Ensure data source revision column", () => {
    ensureDataSourceRevision(db);
  });
  runSchemaMigration(db, "0003_artifact_file_asset_ref", "Ensure artifact file asset ref column", () => {
    ensureArtifactFileAssetRefColumn(db);
  });
  runSchemaMigration(db, "0004_session_title_columns", "Ensure session title metadata columns", () => {
    ensureSessionTitleColumns(db);
  });
  runSchemaMigration(db, "0005_user_scoped_identity", "Migrate identity tables to user-scoped primary keys", () => {
    if (requiresUserScopedIdentityMigration(db)) {
      migrateUserScopedIdentity(db);
    }
  });
  runSchemaMigration(
    db,
    "0006_user_scoped_datasources",
    "Migrate datasource tables to user-scoped primary keys",
    () => {
      if (requiresUserScopedDataSourcesMigration(db)) {
        migrateUserScopedDataSources(db);
      }
    }
  );
  runSchemaMigration(db, "0007_metadata_indexes", "Ensure metadata indexes", () => {
    createMetadataIndexes(db);
  });
  runSchemaMigration(db, "0008_config_schema", "Ensure configuration schema", () => {
    initializeConfigSchema(db);
  });
  runSchemaMigration(db, "0009_interaction_interrupt_event", "Ensure interaction interrupt event column", () => {
    ensureInteractionInterruptEventColumn(db);
  });
  runSchemaMigration(db, "0010_auth_schema", "Ensure password auth metadata schema", () => {
    initializeAuthSchema(db);
  });
  runSchemaMigration(db, "0011_session_branches", "Ensure conversation branch lineage schema", () => {
    initializeSessionBranchSchema(db);
  });
  runSchemaMigration(db, "0012_session_branch_checkpoints", "Ensure checkpoint branch target column", () => {
    ensureSessionBranchCheckpointColumn(db);
  });
  runSchemaMigration(db, "0013_artifact_versions", "Ensure artifact version history schema", () => {
    initializeArtifactVersionSchema(db);
  });
  runSchemaMigration(db, "0014_trace_sections", "Ensure semantic trace section schema", () => {
    initializeTraceSectionSchema(db);
  });
  runSchemaMigration(db, "0015_trace_section_phase_key", "Ensure trace section phase key", () => {
    ensureTraceSectionPhaseKeyColumn(db);
  });
  runSchemaMigration(db, "0016_protocol_state_snapshots", "Ensure protocol state snapshot schema", () => {
    initializeProtocolStateSnapshotSchema(db);
  });
  runSchemaMigration(db, "0017_protocol_event_journal", "Ensure protocol event journal schema", () => {
    initializeProtocolEventJournalSchema(db);
  });
};

const initializeSchemaMigrationTable = (db: DatabaseSync): void => {
  db.exec(`
    CREATE TABLE IF NOT EXISTS schema_migrations (
      id TEXT PRIMARY KEY,
      description TEXT NOT NULL,
      applied_at TEXT NOT NULL
    );
  `);
};

const runSchemaMigration = (
  db: DatabaseSync,
  id: string,
  description: string,
  runner: () => void
): void => {
  runner();
  recordSchemaMigration(db, id, description);
};

const recordSchemaMigration = (db: DatabaseSync, id: string, description: string): void => {
  db.prepare(`
    INSERT OR IGNORE INTO schema_migrations (id, description, applied_at)
    VALUES (?, ?, ?)
  `).run(id, description, new Date().toISOString());
};

const requiresUserScopedIdentityMigration = (db: DatabaseSync): boolean => {
  const primaryKeyColumns = db
    .prepare("PRAGMA table_info(sessions)")
    .all()
    .filter((row) => isRecord(row) && typeof row.pk === "number" && row.pk > 0)
    .sort((left, right) => Number((left as Record<string, unknown>).pk) - Number((right as Record<string, unknown>).pk))
    .map((row) => (row as Record<string, unknown>).name);

  return primaryKeyColumns.join(",") !== "user_id,id";
};

const requiresUserScopedDataSourcesMigration = (db: DatabaseSync): boolean => {
  const primaryKeyColumns = db
    .prepare("PRAGMA table_info(data_sources)")
    .all()
    .filter((row) => isRecord(row) && typeof row.pk === "number" && row.pk > 0)
    .sort((left, right) => Number((left as Record<string, unknown>).pk) - Number((right as Record<string, unknown>).pk))
    .map((row) => (row as Record<string, unknown>).name);

  return primaryKeyColumns.join(",") !== "user_id,id";
};

const ensureDataSourceRevision = (db: DatabaseSync): void => {
  const hasRevision = db.prepare("PRAGMA table_info(data_sources)").all()
    .some((row) => isRecord(row) && row.name === "revision");
  if (!hasRevision) {
    db.exec("ALTER TABLE data_sources ADD COLUMN revision INTEGER NOT NULL DEFAULT 1");
  }
};

const ensureArtifactFileAssetRefColumn = (db: DatabaseSync): void => {
  const hasColumn = db.prepare("PRAGMA table_info(artifacts)").all()
    .some((row) => isRecord(row) && row.name === "file_asset_ref_id");
  if (!hasColumn) {
    db.exec("ALTER TABLE artifacts ADD COLUMN file_asset_ref_id TEXT");
  }
};

const ensureSessionTitleColumns = (db: DatabaseSync): void => {
  const columns = db.prepare("PRAGMA table_info(sessions)").all();
  const hasTitleSource = columns.some((row) => isRecord(row) && row.name === "title_source");
  const hasLastMessageAt = columns.some((row) => isRecord(row) && row.name === "last_message_at");
  if (!hasTitleSource) {
    db.exec("ALTER TABLE sessions ADD COLUMN title_source TEXT");
  }
  if (!hasLastMessageAt) {
    db.exec("ALTER TABLE sessions ADD COLUMN last_message_at TEXT");
  }
};

const ensureInteractionInterruptEventColumn = (db: DatabaseSync): void => {
  const hasColumn = db.prepare("PRAGMA table_info(interactions)").all()
    .some((row) => isRecord(row) && row.name === "interrupt_event_json");
  if (!hasColumn) {
    db.exec("ALTER TABLE interactions ADD COLUMN interrupt_event_json TEXT");
  }
};

const ensureSessionBranchCheckpointColumn = (db: DatabaseSync): void => {
  ensureColumn(db, "session_branches", "fork_checkpoint_id", "TEXT");
  db.exec(`
    CREATE INDEX IF NOT EXISTS idx_session_branches_parent_checkpoint
      ON session_branches(user_id, parent_session_id, fork_checkpoint_id, created_at);
  `);
};

const ensureTraceSectionPhaseKeyColumn = (db: DatabaseSync): void => {
  ensureColumn(db, "trace_sections", "phase_key", "TEXT");
};

const initializeTraceSectionSchema = (db: DatabaseSync): void => {
  db.exec(`
    CREATE TABLE IF NOT EXISTS trace_sections (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      session_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      branch_id TEXT NOT NULL,
      phase_key TEXT,
      start_event_seq INTEGER NOT NULL,
      end_event_seq INTEGER NOT NULL,
      status TEXT NOT NULL,
      title TEXT NOT NULL,
      summary TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (user_id, id),
      FOREIGN KEY (user_id, session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_trace_sections_user_session
      ON trace_sections(user_id, session_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_trace_sections_user_run
      ON trace_sections(user_id, run_id, start_event_seq, end_event_seq);
  `);
};

const initializeProtocolStateSnapshotSchema = (db: DatabaseSync): void => {
  db.exec(`
    CREATE TABLE IF NOT EXISTS protocol_state_snapshots (
      user_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      segment_id TEXT NOT NULL,
      protocol_id TEXT NOT NULL,
      protocol_version TEXT NOT NULL,
      revision INTEGER NOT NULL,
      phase TEXT NOT NULL,
      status TEXT NOT NULL,
      context_package_id TEXT NOT NULL,
      context_package_revision INTEGER NOT NULL,
      state_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (user_id, run_id, segment_id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id),
      FOREIGN KEY (user_id, context_package_id, context_package_revision)
        REFERENCES context_package_snapshots(user_id, package_id, revision)
    );
    CREATE INDEX IF NOT EXISTS idx_protocol_state_snapshots_user_run
      ON protocol_state_snapshots(user_id, run_id, updated_at);
  `);
};

const initializeProtocolEventJournalSchema = (db: DatabaseSync): void => {
  db.exec(`
    CREATE TABLE IF NOT EXISTS protocol_event_journal (
      user_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      segment_id TEXT NOT NULL,
      event_id TEXT NOT NULL,
      revision INTEGER NOT NULL,
      event_type TEXT NOT NULL,
      event_json TEXT NOT NULL,
      created_at TEXT NOT NULL,
      published_at TEXT,
      PRIMARY KEY (user_id, event_id),
      FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_protocol_event_journal_pending
      ON protocol_event_journal(user_id, run_id, published_at, created_at);
  `);
};

const initializeAuthSchema = (db: DatabaseSync): void => {
  ensureColumn(db, "users", "email_verified_at", "TEXT");
  ensureColumn(db, "users", "disabled_at", "TEXT");
  ensureColumn(db, "users", "password_updated_at", "TEXT");
  db.exec(`
    CREATE TABLE IF NOT EXISTS user_password_credentials (
      user_id TEXT PRIMARY KEY,
      password_hash TEXT NOT NULL,
      password_hash_params TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS auth_sessions (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      token_hash TEXT NOT NULL UNIQUE,
      csrf_token_hash TEXT NOT NULL,
      user_agent TEXT,
      ip_address TEXT,
      expires_at TEXT NOT NULL,
      revoked_at TEXT,
      created_at TEXT NOT NULL,
      last_seen_at TEXT NOT NULL,
      FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
      ON auth_sessions(user_id, revoked_at, expires_at);

    CREATE TABLE IF NOT EXISTS auth_tokens (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      purpose TEXT NOT NULL,
      token_hash TEXT NOT NULL UNIQUE,
      expires_at TEXT NOT NULL,
      consumed_at TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_auth_tokens_lookup
      ON auth_tokens(purpose, token_hash, consumed_at, expires_at);

    CREATE TABLE IF NOT EXISTS workspaces (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      kind TEXT NOT NULL,
      owner_user_id TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY (owner_user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_workspaces_owner
      ON workspaces(owner_user_id, kind);

    CREATE TABLE IF NOT EXISTS workspace_memberships (
      workspace_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      role TEXT NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (workspace_id, user_id),
      FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
      FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_workspace_memberships_user
      ON workspace_memberships(user_id, workspace_id);

    CREATE TABLE IF NOT EXISTS auth_audit_events (
      id TEXT PRIMARY KEY,
      user_id TEXT,
      email TEXT,
      event_type TEXT NOT NULL,
      ip_address TEXT,
      user_agent TEXT,
      metadata_json TEXT,
      created_at TEXT NOT NULL,
      FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_auth_audit_events_user
      ON auth_audit_events(user_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_auth_audit_events_email
      ON auth_audit_events(email, created_at DESC);

    CREATE TABLE IF NOT EXISTS auth_rate_limits (
      bucket TEXT PRIMARY KEY,
      count INTEGER NOT NULL,
      reset_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
  `);
};

const initializeSessionBranchSchema = (db: DatabaseSync): void => {
  db.exec(`
    CREATE TABLE IF NOT EXISTS session_branches (
      id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      child_session_id TEXT NOT NULL,
      parent_session_id TEXT NOT NULL,
      root_session_id TEXT NOT NULL,
      fork_run_id TEXT NOT NULL,
      fork_checkpoint_id TEXT,
      fork_message_end_position INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      PRIMARY KEY (user_id, id),
      UNIQUE (user_id, child_session_id),
      FOREIGN KEY (user_id, child_session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, parent_session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, root_session_id) REFERENCES sessions(user_id, id),
      FOREIGN KEY (user_id, fork_run_id) REFERENCES runs(user_id, id)
    );
    CREATE INDEX IF NOT EXISTS idx_session_branches_parent_fork
      ON session_branches(user_id, parent_session_id, fork_run_id, created_at);
  `);
};

const initializeArtifactVersionSchema = (db: DatabaseSync): void => {
  db.exec(`
    CREATE TABLE IF NOT EXISTS artifact_versions (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      artifact_id TEXT NOT NULL,
      version INTEGER NOT NULL,
      file_asset_ref_id TEXT,
      preview_json TEXT,
      content_hash TEXT,
      created_at TEXT NOT NULL,
      UNIQUE (user_id, artifact_id, version),
      FOREIGN KEY (artifact_id) REFERENCES artifacts(id),
      FOREIGN KEY (file_asset_ref_id) REFERENCES file_asset_refs(id)
    );
    CREATE INDEX IF NOT EXISTS idx_artifact_versions_artifact
      ON artifact_versions(user_id, artifact_id, version);
  `);
};

const ensureColumn = (db: DatabaseSync, table: string, column: string, definition: string): void => {
  const hasColumn = db.prepare(`PRAGMA table_info(${table})`).all()
    .some((row) => isRecord(row) && row.name === column);
  if (!hasColumn) {
    db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
  }
};

const migrateUserScopedIdentity = (db: DatabaseSync): void => {
  db.exec("PRAGMA foreign_keys = OFF");

  try {
    db.exec(`
      BEGIN IMMEDIATE;

      CREATE TABLE sessions_user_scoped (
        id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        title TEXT,
        title_source TEXT,
        last_message_at TEXT,
        selected_datasource_id TEXT,
        selected_collection_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, id),
        FOREIGN KEY (user_id) REFERENCES users(id)
      );

      CREATE TABLE runs_user_scoped (
        id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        parent_run_id TEXT,
        request_fingerprint TEXT,
        status TEXT NOT NULL,
        user_input TEXT NOT NULL,
        model_provider TEXT,
        model_name TEXT,
        datasource_id TEXT,
        collection_id TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        error_message TEXT,
        PRIMARY KEY (user_id, id),
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (user_id, session_id) REFERENCES sessions_user_scoped(user_id, id),
        FOREIGN KEY (user_id, parent_run_id) REFERENCES runs_user_scoped(user_id, id)
      );

      CREATE TABLE run_events_user_scoped (
        id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, run_id, seq),
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (user_id, run_id) REFERENCES runs_user_scoped(user_id, id),
        FOREIGN KEY (user_id, session_id) REFERENCES sessions_user_scoped(user_id, id)
      );

      CREATE TABLE artifacts_user_scoped (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        type TEXT NOT NULL,
        name TEXT NOT NULL,
        mime_type TEXT,
        storage_path TEXT,
        file_asset_ref_id TEXT,
        preview_json TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (user_id, session_id) REFERENCES sessions_user_scoped(user_id, id),
        FOREIGN KEY (user_id, run_id) REFERENCES runs_user_scoped(user_id, id)
      );

      CREATE TABLE sql_audit_logs_user_scoped (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        run_id TEXT,
        datasource_id TEXT NOT NULL,
        sql_text TEXT NOT NULL,
        status TEXT NOT NULL,
        blocked_reason TEXT,
        row_count INTEGER,
        elapsed_ms INTEGER,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (user_id, run_id) REFERENCES runs_user_scoped(user_id, id),
        FOREIGN KEY (datasource_id) REFERENCES data_sources(id)
      );

      INSERT INTO sessions_user_scoped (
        id, user_id, title, title_source, last_message_at, selected_datasource_id,
        selected_collection_id, created_at, updated_at
      )
      SELECT
        id, user_id, title, title_source, last_message_at, selected_datasource_id,
        selected_collection_id, created_at, updated_at
      FROM sessions;
      INSERT INTO runs_user_scoped (
        id, user_id, session_id, status, user_input, model_provider, model_name,
        datasource_id, collection_id, started_at, finished_at, error_message
      )
      SELECT
        id, user_id, session_id, status, user_input, model_provider, model_name,
        datasource_id, collection_id, started_at, finished_at, error_message
      FROM runs;
      INSERT INTO run_events_user_scoped SELECT * FROM run_events;
      INSERT INTO artifacts_user_scoped (
        id, user_id, session_id, run_id, type, name, mime_type, storage_path,
        file_asset_ref_id, preview_json, metadata_json, created_at
      )
      SELECT
        id, user_id, session_id, run_id, type, name, mime_type, storage_path,
        NULL, preview_json, metadata_json, created_at
      FROM artifacts;
      INSERT INTO sql_audit_logs_user_scoped SELECT * FROM sql_audit_logs;

      DROP TABLE run_events;
      DROP TABLE artifacts;
      DROP TABLE sql_audit_logs;
      DROP TABLE runs;
      DROP TABLE sessions;

      ALTER TABLE sessions_user_scoped RENAME TO sessions;
      ALTER TABLE runs_user_scoped RENAME TO runs;
      ALTER TABLE run_events_user_scoped RENAME TO run_events;
      ALTER TABLE artifacts_user_scoped RENAME TO artifacts;
      ALTER TABLE sql_audit_logs_user_scoped RENAME TO sql_audit_logs;

      COMMIT;
    `);
  } catch (error) {
    try {
      db.exec("ROLLBACK");
    } catch {
      // The migration may have failed before opening the transaction.
    }
    throw error;
  } finally {
    db.exec("PRAGMA foreign_keys = ON");
  }

  const violations = db.prepare("PRAGMA foreign_key_check").all();

  if (violations.length > 0) {
    throw new Error(`Metadata identity migration produced ${violations.length} foreign key violation(s)`);
  }
};

const migrateUserScopedDataSources = (db: DatabaseSync): void => {
  db.exec("PRAGMA foreign_keys = OFF");

  try {
    db.exec(`
      BEGIN IMMEDIATE;

      CREATE TABLE data_sources_user_scoped (
        id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        config_json TEXT NOT NULL,
        credential_ref TEXT,
        description TEXT,
        status TEXT NOT NULL,
        last_test_at TEXT,
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, id),
        FOREIGN KEY (user_id) REFERENCES users(id)
      );

      CREATE TABLE sql_audit_logs_datasource_scoped (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        run_id TEXT,
        datasource_id TEXT NOT NULL,
        sql_text TEXT NOT NULL,
        status TEXT NOT NULL,
        blocked_reason TEXT,
        row_count INTEGER,
        elapsed_ms INTEGER,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (user_id, run_id) REFERENCES runs(user_id, id),
        FOREIGN KEY (user_id, datasource_id) REFERENCES data_sources_user_scoped(user_id, id)
      );

      INSERT INTO data_sources_user_scoped (
        id, user_id, name, type, config_json, credential_ref, description, status,
        last_test_at, revision, created_at, updated_at
      )
      SELECT
        id, user_id, name, type, config_json, credential_ref, description, status,
        last_test_at, revision, created_at, updated_at
      FROM data_sources;

      INSERT INTO sql_audit_logs_datasource_scoped SELECT * FROM sql_audit_logs;

      DROP TABLE sql_audit_logs;
      DROP TABLE data_sources;

      ALTER TABLE data_sources_user_scoped RENAME TO data_sources;
      ALTER TABLE sql_audit_logs_datasource_scoped RENAME TO sql_audit_logs;

      COMMIT;
    `);
  } catch (error) {
    try {
      db.exec("ROLLBACK");
    } catch {
      // The migration may have failed before opening the transaction.
    }
    throw error;
  } finally {
    db.exec("PRAGMA foreign_keys = ON");
  }

  const violations = db.prepare("PRAGMA foreign_key_check").all();

  if (violations.length > 0) {
    throw new Error(`Metadata datasource migration produced ${violations.length} foreign key violation(s)`);
  }
};

const createMetadataIndexes = (db: DatabaseSync): void => {
  db.exec(`
    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_session_branches_parent_fork
      ON session_branches(user_id, parent_session_id, fork_run_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_data_sources_user ON data_sources(user_id);
    CREATE INDEX IF NOT EXISTS idx_runs_user_session ON runs(user_id, session_id);
    CREATE INDEX IF NOT EXISTS idx_run_events_user_run ON run_events(user_id, run_id);
    CREATE INDEX IF NOT EXISTS idx_conversation_messages_user_session
      ON conversation_messages(user_id, session_id, position);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_messages_message_id
      ON conversation_messages(user_id, session_id, message_id)
      WHERE message_id IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_messages_run_hash
      ON conversation_messages(user_id, session_id, run_id, role, content_hash);
    CREATE INDEX IF NOT EXISTS idx_conversation_summaries_user_session
      ON conversation_summaries(user_id, session_id, to_position);
    CREATE INDEX IF NOT EXISTS idx_long_term_memories_user_scope
      ON long_term_memories(user_id, scope, status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_long_term_memories_user_session
      ON long_term_memories(user_id, session_id, status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_long_term_memories_user_datasource
      ON long_term_memories(user_id, datasource_id, status, updated_at);
    CREATE INDEX IF NOT EXISTS idx_file_assets_sha256 ON file_assets(sha256);
    CREATE INDEX IF NOT EXISTS idx_file_asset_refs_scope
      ON file_asset_refs(user_id, workspace_id, status, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_file_asset_refs_asset
      ON file_asset_refs(file_asset_id, status);
    CREATE INDEX IF NOT EXISTS idx_artifacts_user_run ON artifacts(user_id, run_id);
    CREATE INDEX IF NOT EXISTS idx_artifacts_user_session ON artifacts(user_id, session_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_sql_audit_logs_user_run ON sql_audit_logs(user_id, run_id);
    CREATE INDEX IF NOT EXISTS idx_sql_audit_logs_user_datasource ON sql_audit_logs(user_id, datasource_id);
  `);
};

type Optional<T> = T | undefined;

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

const optionalString = (value: unknown): Optional<string> => (typeof value === "string" ? value : undefined);

const optionalNumber = (value: unknown): Optional<number> => (typeof value === "number" ? value : undefined);

const contextPackageSnapshotId = (packageId: string, revision: number): string => `${packageId}:${revision}`;

const clampConfidence = (value: number): number => Math.max(0, Math.min(1, value));

const createLongTermMemoryHash = (input: CreateLongTermMemoryInput): string =>
  createHash("sha256")
    .update(JSON.stringify({
      content_text: input.content_text,
      datasource_id: input.datasource_id ?? null,
      kind: input.kind,
      scope: input.scope,
      session_id: input.session_id ?? null
    }))
    .digest("hex");

const tokenizeMemoryText = (text: string): string[] =>
  [...new Set(text.toLowerCase().split(/[^\p{L}\p{N}_]+/u).filter((term) => term.length >= 2))];

const scoreLongTermMemory = (
  memory: LongTermMemoryRecord,
  queryTerms: string[],
  input: ListRelevantLongTermMemoriesInput
): number => {
  const content = `${memory.kind} ${memory.content_text}`.toLowerCase();
  const lexicalScore = queryTerms.reduce((score, term) => score + (content.includes(term) ? 1 : 0), 0);
  const scopeScore =
    memory.scope === "session" && memory.session_id === input.session_id
      ? 3
      : memory.scope === "datasource" && memory.datasource_id === input.datasource_id
        ? 2
        : 1;
  const queryScore = queryTerms.length === 0 ? 1 : lexicalScore;
  return queryScore > 0 ? queryScore + scopeScore + memory.confidence : 0;
};

const requiredString = (row: Record<string, unknown>, key: string): string => {
  const value = row[key];

  if (typeof value !== "string") {
    throw new Error(`Expected string column: ${key}`);
  }

  return value;
};

const requiredNumber = (row: Record<string, unknown>, key: string): number => {
  const value = row[key];

  if (typeof value !== "number") {
    throw new Error(`Expected number column: ${key}`);
  }

  return value;
};

const mapUserRow = (row: unknown): Optional<UserRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const email = optionalString(row.email);
  const displayName = optionalString(row.display_name);
  const devToken = optionalString(row.dev_token);
  const emailVerifiedAt = optionalString(row.email_verified_at);
  const disabledAt = optionalString(row.disabled_at);
  const passwordUpdatedAt = optionalString(row.password_updated_at);

  return {
    id: requiredString(row, "id"),
    ...(email ? { email } : {}),
    ...(displayName ? { display_name: displayName } : {}),
    ...(devToken ? { dev_token: devToken } : {}),
    ...(emailVerifiedAt ? { email_verified_at: emailVerifiedAt } : {}),
    ...(disabledAt ? { disabled_at: disabledAt } : {}),
    ...(passwordUpdatedAt ? { password_updated_at: passwordUpdatedAt } : {}),
    created_at: requiredString(row, "created_at"),
    updated_at: requiredString(row, "updated_at")
  };
};

const mapUserPasswordCredentialRow = (row: unknown): Optional<UserPasswordCredentialRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  return {
    user_id: requiredString(row, "user_id"),
    password_hash: requiredString(row, "password_hash"),
    password_hash_params: requiredString(row, "password_hash_params"),
    created_at: requiredString(row, "created_at"),
    updated_at: requiredString(row, "updated_at")
  };
};

const mapAuthSessionRow = (row: unknown): Optional<AuthSessionRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  const userAgent = optionalString(row.user_agent);
  const ipAddress = optionalString(row.ip_address);
  const revokedAt = optionalString(row.revoked_at);
  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    token_hash: requiredString(row, "token_hash"),
    csrf_token_hash: requiredString(row, "csrf_token_hash"),
    ...(userAgent ? { user_agent: userAgent } : {}),
    ...(ipAddress ? { ip_address: ipAddress } : {}),
    expires_at: requiredString(row, "expires_at"),
    ...(revokedAt ? { revoked_at: revokedAt } : {}),
    created_at: requiredString(row, "created_at"),
    last_seen_at: requiredString(row, "last_seen_at")
  };
};

const mapAuthTokenRow = (row: unknown): Optional<AuthTokenRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  const consumedAt = optionalString(row.consumed_at);
  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    purpose: requiredString(row, "purpose") as AuthTokenPurpose,
    token_hash: requiredString(row, "token_hash"),
    expires_at: requiredString(row, "expires_at"),
    ...(consumedAt ? { consumed_at: consumedAt } : {}),
    created_at: requiredString(row, "created_at")
  };
};

const mapWorkspaceRow = (row: unknown): Optional<WorkspaceRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  return {
    id: requiredString(row, "id"),
    name: requiredString(row, "name"),
    kind: requiredString(row, "kind") as WorkspaceRecord["kind"],
    owner_user_id: requiredString(row, "owner_user_id"),
    created_at: requiredString(row, "created_at"),
    updated_at: requiredString(row, "updated_at")
  };
};

const mapWorkspaceMembershipRow = (row: unknown): Optional<WorkspaceMembershipRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  return {
    workspace_id: requiredString(row, "workspace_id"),
    user_id: requiredString(row, "user_id"),
    role: requiredString(row, "role") as WorkspaceMembershipRecord["role"],
    created_at: requiredString(row, "created_at")
  };
};

const mapAuthAuditEventRow = (row: unknown): Optional<AuthAuditEventRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  const userId = optionalString(row.user_id);
  const email = optionalString(row.email);
  const ipAddress = optionalString(row.ip_address);
  const userAgent = optionalString(row.user_agent);
  const metadataJson = optionalString(row.metadata_json);
  return {
    id: requiredString(row, "id"),
    ...(userId ? { user_id: userId } : {}),
    ...(email ? { email } : {}),
    event_type: requiredString(row, "event_type"),
    ...(ipAddress ? { ip_address: ipAddress } : {}),
    ...(userAgent ? { user_agent: userAgent } : {}),
    ...(metadataJson ? { metadata_json: metadataJson } : {}),
    created_at: requiredString(row, "created_at")
  };
};

const mapSessionRow = (row: unknown): Optional<SessionRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const title = optionalString(row.title);
  const titleSource = sessionTitleSource(row.title_source);
  const lastMessageAt = optionalString(row.last_message_at);
  const selectedDatasourceId = optionalString(row.selected_datasource_id);
  const selectedCollectionId = optionalString(row.selected_collection_id);

  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    ...(title ? { title } : {}),
    ...(titleSource ? { title_source: titleSource } : {}),
    ...(lastMessageAt ? { last_message_at: lastMessageAt } : {}),
    ...(selectedDatasourceId ? { selected_datasource_id: selectedDatasourceId } : {}),
    ...(selectedCollectionId ? { selected_collection_id: selectedCollectionId } : {}),
    created_at: requiredString(row, "created_at"),
    updated_at: requiredString(row, "updated_at")
  };
};

const mapQueryHistoryRow = (row: unknown): Optional<QueryHistoryRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  const runId = optionalString(row.run_id);
  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    workspace_id: requiredString(row, "workspace_id"),
    session_id: requiredString(row, "session_id"),
    ...(runId ? { run_id: runId } : {}),
    datasource_id: requiredString(row, "datasource_id"),
    sql_text: requiredString(row, "sql_text"),
    row_count: requiredNumber(row, "row_count"),
    elapsed_ms: requiredNumber(row, "elapsed_ms"),
    favorite: Boolean(requiredNumber(row, "favorite")),
    created_at: requiredString(row, "created_at"),
    updated_at: requiredString(row, "updated_at")
  };
};

const mapRequiredQueryHistoryRow = (row: unknown): QueryHistoryRecord => {
  const record = mapQueryHistoryRow(row);
  if (!record) {
    throw new Error("Expected query history row");
  }
  return record;
};

const sessionTitleSource = (value: unknown): Optional<SessionRecord["title_source"]> =>
  value === "llm" || value === "fallback" || value === "user" ? value : undefined;

const decodeSessionCursor = (cursor: string): Optional<{ id: string; sort_at: string }> => {
  try {
    const decoded = JSON.parse(Buffer.from(cursor, "base64url").toString("utf8")) as unknown;
    if (!isRecord(decoded)) {
      return undefined;
    }
    const sortAt = optionalString(decoded.sort_at);
    const id = optionalString(decoded.id);
    return sortAt && id ? { id, sort_at: sortAt } : undefined;
  } catch {
    return undefined;
  }
};

const mapRequiredSessionRow = (row: unknown): SessionRecord => {
  const session = mapSessionRow(row);

  if (!session) {
    throw new Error("Invalid session row");
  }

  return session;
};

const mapSessionBranchRow = (row: unknown): Optional<SessionBranchRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  const forkCheckpointId = optionalString(row.fork_checkpoint_id);
  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    child_session_id: requiredString(row, "child_session_id"),
    parent_session_id: requiredString(row, "parent_session_id"),
    root_session_id: requiredString(row, "root_session_id"),
    fork_run_id: requiredString(row, "fork_run_id"),
    ...(forkCheckpointId ? { fork_checkpoint_id: forkCheckpointId } : {}),
    fork_message_end_position: requiredNumber(row, "fork_message_end_position"),
    created_at: requiredString(row, "created_at")
  };
};

const mapRequiredSessionBranchRow = (row: unknown): SessionBranchRecord => {
  const branch = mapSessionBranchRow(row);
  if (!branch) {
    throw new Error("Invalid session branch row");
  }
  return branch;
};

const mapRunRow = (row: unknown): Optional<RunRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const modelProvider = optionalString(row.model_provider);
  const modelName = optionalString(row.model_name);
  const parentRunId = optionalString(row.parent_run_id);
  const requestFingerprint = optionalString(row.request_fingerprint);
  const datasourceId = optionalString(row.datasource_id);
  const collectionId = optionalString(row.collection_id);
  const finishedAt = optionalString(row.finished_at);
  const errorMessage = optionalString(row.error_message);

  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    session_id: requiredString(row, "session_id"),
    ...(parentRunId ? { parent_run_id: parentRunId } : {}),
    ...(requestFingerprint ? { request_fingerprint: requestFingerprint } : {}),
    status: requiredString(row, "status") as RunRecord["status"],
    user_input: requiredString(row, "user_input"),
    ...(modelProvider ? { model_provider: modelProvider } : {}),
    ...(modelName ? { model_name: modelName } : {}),
    ...(datasourceId ? { datasource_id: datasourceId } : {}),
    ...(collectionId ? { collection_id: collectionId } : {}),
    started_at: requiredString(row, "started_at"),
    ...(finishedAt ? { finished_at: finishedAt } : {}),
    ...(errorMessage ? { error_message: errorMessage } : {})
  };
};

const mapInteractionRow = (row: unknown): Optional<InteractionRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  const resumeFingerprint = optionalString(row.resume_fingerprint);
  const responseJson = optionalString(row.response_json);
  const resolvedAt = optionalString(row.resolved_at);
  const interruptEventJson = optionalString(row.interrupt_event_json);
  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    session_id: requiredString(row, "session_id"),
    run_id: requiredString(row, "run_id"),
    tool_call_id: requiredString(row, "tool_call_id"),
    tool_name: requiredString(row, "tool_name") as InteractionRecord["tool_name"],
    payload_json: requiredString(row, "payload_json"),
    ...(interruptEventJson ? { interrupt_event_json: interruptEventJson } : {}),
    status: requiredString(row, "status") as InteractionRecord["status"],
    ...(resumeFingerprint ? { resume_fingerprint: resumeFingerprint } : {}),
    ...(responseJson ? { response_json: responseJson } : {}),
    created_at: requiredString(row, "created_at"),
    ...(resolvedAt ? { resolved_at: resolvedAt } : {})
  };
};

const mapDataSourceRow = (row: unknown): Optional<DataSourceRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const credentialRef = optionalString(row.credential_ref);
  const description = optionalString(row.description);
  const lastTestAt = optionalString(row.last_test_at);

  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    name: requiredString(row, "name"),
    type: requiredString(row, "type"),
    config_json: requiredString(row, "config_json"),
    ...(credentialRef ? { credential_ref: credentialRef } : {}),
    ...(description ? { description } : {}),
    status: requiredString(row, "status") as DataSourceRecord["status"],
    ...(lastTestAt ? { last_test_at: lastTestAt } : {}),
    revision: requiredNumber(row, "revision"),
    created_at: requiredString(row, "created_at"),
    updated_at: requiredString(row, "updated_at")
  };
};

const mapRequiredDataSourceRow = (row: unknown): DataSourceRecord => {
  const dataSource = mapDataSourceRow(row);

  if (!dataSource) {
    throw new Error("Invalid data source row");
  }

  return dataSource;
};

const mapRunEventRow = (row: unknown): Optional<RunEventRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    run_id: requiredString(row, "run_id"),
    session_id: requiredString(row, "session_id"),
    seq: requiredNumber(row, "seq"),
    event_type: requiredString(row, "event_type") as EventType,
    payload_json: requiredString(row, "payload_json"),
    created_at: requiredString(row, "created_at")
  };
};

const mapRequiredRunEventRow = (row: unknown): RunEventRecord => {
  const event = mapRunEventRow(row);

  if (!event) {
    throw new Error("Invalid run event row");
  }

  return event;
};

const mapConversationMessageRow = (row: unknown): Optional<ConversationMessageRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const messageId = optionalString(row.message_id);

  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    session_id: requiredString(row, "session_id"),
    run_id: requiredString(row, "run_id"),
    role: requiredString(row, "role") as ConversationMessageRole,
    source: requiredString(row, "source") as ConversationMessageSource,
    ...(messageId ? { message_id: messageId } : {}),
    content_json: requiredString(row, "content_json"),
    content_text: requiredString(row, "content_text"),
    content_hash: requiredString(row, "content_hash"),
    position: requiredNumber(row, "position"),
    created_at: requiredString(row, "created_at")
  };
};

const mapRequiredConversationMessageRow = (row: unknown): ConversationMessageRecord => {
  const message = mapConversationMessageRow(row);

  if (!message) {
    throw new Error("Invalid conversation message row");
  }

  return message;
};

const mapConversationSummaryRow = (row: unknown): Optional<ConversationSummaryRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const sourceRunId = optionalString(row.source_run_id);

  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    session_id: requiredString(row, "session_id"),
    ...(sourceRunId ? { source_run_id: sourceRunId } : {}),
    from_position: requiredNumber(row, "from_position"),
    to_position: requiredNumber(row, "to_position"),
    summary_text: requiredString(row, "summary_text"),
    summary_hash: requiredString(row, "summary_hash"),
    created_at: requiredString(row, "created_at")
  };
};

const mapLongTermMemoryRow = (row: unknown): Optional<LongTermMemoryRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const datasourceId = optionalString(row.datasource_id);
  const lastAccessedAt = optionalString(row.last_accessed_at);
  const sessionId = optionalString(row.session_id);
  const source = optionalString(row.source);
  const sourceRunId = optionalString(row.source_run_id);

  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    scope: requiredString(row, "scope") as LongTermMemoryScope,
    ...(sessionId ? { session_id: sessionId } : {}),
    ...(datasourceId ? { datasource_id: datasourceId } : {}),
    kind: requiredString(row, "kind"),
    content_json: requiredString(row, "content_json"),
    content_text: requiredString(row, "content_text"),
    memory_hash: requiredString(row, "memory_hash"),
    confidence: requiredNumber(row, "confidence"),
    status: requiredString(row, "status") as LongTermMemoryStatus,
    ...(source ? { source } : {}),
    ...(sourceRunId ? { source_run_id: sourceRunId } : {}),
    created_at: requiredString(row, "created_at"),
    updated_at: requiredString(row, "updated_at"),
    ...(lastAccessedAt ? { last_accessed_at: lastAccessedAt } : {})
  };
};

const mapRequiredLongTermMemoryRow = (row: unknown): LongTermMemoryRecord => {
  const memory = mapLongTermMemoryRow(row);

  if (!memory) {
    throw new Error("Invalid long-term memory row");
  }

  return memory;
};

const mapContextPackageSnapshotRow = (row: unknown): Optional<ContextPackageSnapshotRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const planJson = optionalString(row.plan_json);
  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    session_id: requiredString(row, "session_id"),
    run_id: requiredString(row, "run_id"),
    package_id: requiredString(row, "package_id"),
    revision: requiredNumber(row, "revision"),
    payload_json: requiredString(row, "payload_json"),
    ...(planJson ? { plan_json: planJson } : {}),
    created_at: requiredString(row, "created_at")
  };
};

const mapProtocolStateSnapshotRow = (row: unknown): Optional<ProtocolStateSnapshotRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  return {
    user_id: requiredString(row, "user_id"),
    run_id: requiredString(row, "run_id"),
    segment_id: requiredString(row, "segment_id"),
    protocol_id: requiredString(row, "protocol_id"),
    protocol_version: requiredString(row, "protocol_version"),
    revision: requiredNumber(row, "revision"),
    phase: requiredString(row, "phase"),
    status: requiredString(row, "status"),
    context_package_id: requiredString(row, "context_package_id"),
    context_package_revision: requiredNumber(row, "context_package_revision"),
    state_json: requiredString(row, "state_json"),
    created_at: requiredString(row, "created_at"),
    updated_at: requiredString(row, "updated_at")
  };
};

const requireProtocolStateFields = (value: unknown): {
  protocolId: string;
  protocolVersion: string;
  runId: string;
  segmentId: string;
  revision: number;
  phase: string;
  status: string;
  contextPackageId: string;
  contextPackageRevision: number;
} => {
  if (!isRecord(value) || !isRecord(value.contextPackageRef)) {
    throw new Error("PROTOCOL_STATE_INVALID");
  }
  return {
    protocolId: requiredString(value, "protocolId"),
    protocolVersion: requiredString(value, "protocolVersion"),
    runId: requiredString(value, "runId"),
    segmentId: requiredString(value, "segmentId"),
    revision: requiredNumber(value, "revision"),
    phase: requiredString(value, "phase"),
    status: requiredString(value, "status"),
    contextPackageId: requiredString(value.contextPackageRef, "packageId"),
    contextPackageRevision: requiredNumber(value.contextPackageRef, "revision")
  };
};

const requireProtocolEventFields = (value: unknown): {
  eventId: string;
  runId: string;
  segmentId: string;
  revision: number;
  type: string;
} => {
  if (!isRecord(value)) {
    throw new Error("PROTOCOL_EVENT_INVALID");
  }
  return {
    eventId: requiredString(value, "eventId"),
    runId: requiredString(value, "runId"),
    segmentId: requiredString(value, "segmentId"),
    revision: requiredNumber(value, "revision"),
    type: requiredString(value, "type")
  };
};

const mapCheckpointRow = (row: unknown): Optional<CheckpointRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const contextPlanId = optionalString(row.context_plan_id);
  const parentCheckpointId = optionalString(row.parent_checkpoint_id);
  const stepNumber = optionalNumber(row.step_number);
  const stepId = optionalString(row.step_id);
  const toolCallId = optionalString(row.tool_call_id);
  const messagePosition = optionalNumber(row.message_position);
  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    session_id: requiredString(row, "session_id"),
    run_id: requiredString(row, "run_id"),
    branch_id: requiredString(row, "branch_id"),
    event_seq: requiredNumber(row, "event_seq"),
    context_package_id: requiredString(row, "context_package_id"),
    context_package_revision: requiredNumber(row, "context_package_revision"),
    kind: requiredString(row, "kind") as CheckpointKind,
    status: requiredString(row, "status") as CheckpointStatus,
    label: requiredString(row, "label"),
    ...(contextPlanId ? { context_plan_id: contextPlanId } : {}),
    ...(parentCheckpointId ? { parent_checkpoint_id: parentCheckpointId } : {}),
    ...(stepNumber !== undefined ? { step_number: stepNumber } : {}),
    ...(stepId ? { step_id: stepId } : {}),
    ...(toolCallId ? { tool_call_id: toolCallId } : {}),
    ...(messagePosition !== undefined ? { message_position: messagePosition } : {}),
    created_at: requiredString(row, "created_at")
  };
};

const mapRequiredCheckpointRow = (row: unknown): CheckpointRecord => {
  const checkpoint = mapCheckpointRow(row);
  if (!checkpoint) {
    throw new Error("Invalid checkpoint row");
  }
  return checkpoint;
};

const mapTraceSectionRow = (row: unknown): Optional<TraceSectionRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  const id = requiredString(row, "id");
  return {
    id,
    user_id: requiredString(row, "user_id"),
    session_id: requiredString(row, "session_id"),
    run_id: requiredString(row, "run_id"),
    branch_id: requiredString(row, "branch_id"),
    phase_key: optionalString(row.phase_key) ?? `legacy:${id}`,
    start_event_seq: requiredNumber(row, "start_event_seq"),
    end_event_seq: requiredNumber(row, "end_event_seq"),
    status: requiredString(row, "status") as TraceSectionStatus,
    title: requiredString(row, "title"),
    summary: requiredString(row, "summary"),
    created_at: requiredString(row, "created_at"),
    updated_at: requiredString(row, "updated_at")
  };
};

const mapRequiredTraceSectionRow = (row: unknown): TraceSectionRecord => {
  const section = mapTraceSectionRow(row);
  if (!section) {
    throw new Error("Invalid trace section row");
  }
  return section;
};

const mapArtifactRow = (row: unknown): Optional<ArtifactRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const mimeType = optionalString(row.mime_type);
  const storagePath = optionalString(row.storage_path);
  const fileAssetRefId = optionalString(row.file_asset_ref_id);
  const previewJson = optionalString(row.preview_json);
  const metadataJson = optionalString(row.metadata_json);

  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    session_id: requiredString(row, "session_id"),
    run_id: requiredString(row, "run_id"),
    type: requiredString(row, "type") as ArtifactType,
    name: requiredString(row, "name"),
    ...(mimeType ? { mime_type: mimeType } : {}),
    ...(storagePath ? { storage_path: storagePath } : {}),
    ...(fileAssetRefId ? { file_asset_ref_id: fileAssetRefId } : {}),
    ...(previewJson ? { preview_json: previewJson } : {}),
    ...(metadataJson ? { metadata_json: metadataJson } : {}),
    created_at: requiredString(row, "created_at")
  };
};

const mapArtifactVersionRow = (row: unknown): Optional<ArtifactVersionRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  const fileAssetRefId = optionalString(row.file_asset_ref_id);
  const previewJson = optionalString(row.preview_json);
  const contentHash = optionalString(row.content_hash);
  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    artifact_id: requiredString(row, "artifact_id"),
    version: requiredNumber(row, "version"),
    ...(fileAssetRefId ? { file_asset_ref_id: fileAssetRefId } : {}),
    ...(previewJson ? { preview_json: previewJson } : {}),
    ...(contentHash ? { content_hash: contentHash } : {}),
    created_at: requiredString(row, "created_at")
  };
};

const mapFileAssetRow = (row: unknown): Optional<FileAssetRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  const detectedMimeType = optionalString(row.detected_mime_type);
  return {
    id: requiredString(row, "id"),
    sha256: requiredString(row, "sha256"),
    size_bytes: requiredNumber(row, "size_bytes"),
    storage_path: requiredString(row, "storage_path"),
    ...(detectedMimeType ? { detected_mime_type: detectedMimeType } : {}),
    created_at: requiredString(row, "created_at")
  };
};

const mapFileAssetRefRow = (row: unknown): Optional<FileAssetRefRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }
  const declaredMimeType = optionalString(row.declared_mime_type);
  const sessionId = optionalString(row.session_id);
  const runId = optionalString(row.run_id);
  const metadataJson = optionalString(row.metadata_json);
  return {
    id: requiredString(row, "id"),
    file_asset_id: requiredString(row, "file_asset_id"),
    user_id: requiredString(row, "user_id"),
    workspace_id: requiredString(row, "workspace_id"),
    filename: requiredString(row, "filename"),
    ...(declaredMimeType ? { declared_mime_type: declaredMimeType } : {}),
    source: requiredString(row, "source") as FileAssetRefSource,
    status: requiredString(row, "status") as FileAssetRefStatus,
    ...(sessionId ? { session_id: sessionId } : {}),
    ...(runId ? { run_id: runId } : {}),
    ...(metadataJson ? { metadata_json: metadataJson } : {}),
    created_at: requiredString(row, "created_at")
  };
};

const mapRequiredFileAssetRefRow = (row: unknown): FileAssetRefRecord => {
  const ref = mapFileAssetRefRow(row);
  if (!ref) {
    throw new Error("Invalid file asset ref row");
  }
  return ref;
};

const mapSqlAuditLogRow = (row: unknown): Optional<SqlAuditLogRecord> => {
  if (!isRecord(row)) {
    return undefined;
  }

  const runId = optionalString(row.run_id);
  const blockedReason = optionalString(row.blocked_reason);
  const rowCount = optionalNumber(row.row_count);
  const elapsedMs = optionalNumber(row.elapsed_ms);

  return {
    id: requiredString(row, "id"),
    user_id: requiredString(row, "user_id"),
    ...(runId ? { run_id: runId } : {}),
    datasource_id: requiredString(row, "datasource_id"),
    sql_text: requiredString(row, "sql_text"),
    status: requiredString(row, "status") as SqlAuditLogRecord["status"],
    ...(blockedReason ? { blocked_reason: blockedReason } : {}),
    ...(rowCount !== undefined ? { row_count: rowCount } : {}),
    ...(elapsedMs !== undefined ? { elapsed_ms: elapsedMs } : {}),
    created_at: requiredString(row, "created_at")
  };
};

const mapRequiredSqlAuditLogRow = (row: unknown): SqlAuditLogRecord => {
  const log = mapSqlAuditLogRow(row);

  if (!log) {
    throw new Error("Invalid SQL audit log row");
  }

  return log;
};

const mapRequiredArtifactRow = (row: unknown): ArtifactRecord => {
  const artifact = mapArtifactRow(row);

  if (!artifact) {
    throw new Error("Invalid artifact row");
  }

  return artifact;
};

const mapRequiredArtifactVersionRow = (row: unknown): ArtifactVersionRecord => {
  const version = mapArtifactVersionRow(row);

  if (!version) {
    throw new Error("Invalid artifact version row");
  }

  return version;
};
