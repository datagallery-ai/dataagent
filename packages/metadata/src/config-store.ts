import { createCipheriv, createDecipheriv, createHash, randomBytes, randomUUID } from "node:crypto";
import type { DatabaseSync } from "node:sqlite";

export type ConfigResourceKind =
  | "datasource-schema"
  | "knowledge-base"
  | "mcp-server"
  | "model-profile"
  | "skill";

export type ConfigResourceRecord = {
  id: string;
  workspace_id: string;
  user_id: string;
  kind: ConfigResourceKind;
  name: string;
  description?: string;
  payload: Record<string, unknown>;
  secret_ref?: string;
  default_enabled: boolean;
  builtin: boolean;
  status: string;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type UpsertConfigResourceInput = {
  id: string;
  workspace_id: string;
  user_id: string;
  kind: ConfigResourceKind;
  name: string;
  description?: string;
  payload?: Record<string, unknown>;
  secret_ref?: string | null;
  default_enabled?: boolean;
  builtin?: boolean;
  status?: string;
  expected_revision?: number;
};

export type JobStatus = "queued" | "running" | "completed" | "failed" | "canceled" | "interrupted";

export type JobRecord = {
  id: string;
  workspace_id: string;
  user_id: string;
  type: string;
  resource_id?: string;
  status: JobStatus;
  progress: number;
  result?: unknown;
  error?: unknown;
  idempotency_key?: string;
  created_at: string;
  started_at?: string;
  finished_at?: string;
};

export const initializeConfigSchema = (db: DatabaseSync): void => {
  db.exec(`
    CREATE TABLE IF NOT EXISTS config_resources (
      id TEXT NOT NULL,
      workspace_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      name TEXT NOT NULL,
      description TEXT,
      payload_json TEXT NOT NULL,
      secret_ref TEXT,
      default_enabled INTEGER NOT NULL DEFAULT 1,
      builtin INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'untested',
      revision INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (workspace_id, user_id, kind, id),
      FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_config_resources_scope
      ON config_resources(workspace_id, user_id, kind, updated_at DESC);

    CREATE TABLE IF NOT EXISTS encrypted_secrets (
      ref TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      owner_kind TEXT NOT NULL,
      owner_id TEXT NOT NULL,
      iv TEXT NOT NULL,
      auth_tag TEXT NOT NULL,
      ciphertext TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY (user_id) REFERENCES users(id)
    );
    CREATE INDEX IF NOT EXISTS idx_encrypted_secrets_owner
      ON encrypted_secrets(workspace_id, user_id, owner_kind, owner_id);

    CREATE TABLE IF NOT EXISTS config_jobs (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      user_id TEXT NOT NULL,
      type TEXT NOT NULL,
      resource_id TEXT,
      status TEXT NOT NULL,
      progress INTEGER NOT NULL DEFAULT 0,
      result_json TEXT,
      error_json TEXT,
      idempotency_key TEXT,
      created_at TEXT NOT NULL,
      started_at TEXT,
      finished_at TEXT,
      FOREIGN KEY (user_id) REFERENCES users(id),
      UNIQUE (workspace_id, user_id, type, idempotency_key)
    );
    CREATE INDEX IF NOT EXISTS idx_config_jobs_scope
      ON config_jobs(workspace_id, user_id, created_at DESC);
  `);

  db.prepare("UPDATE config_jobs SET status = 'interrupted', finished_at = ? WHERE status = 'running'")
    .run(new Date().toISOString());
};

export class ConfigResourceRepository {
  constructor(private readonly db: DatabaseSync) {}

  /** Create or update one workspace-scoped configuration resource. */
  upsert(input: UpsertConfigResourceInput): ConfigResourceRecord {
    const current = this.find(input);
    if (current?.builtin && input.builtin !== true) {
      throw new Error(`BUILTIN_RESOURCE_READONLY:${input.id}`);
    }
    if (input.expected_revision !== undefined && current?.revision !== input.expected_revision) {
      throw new Error(`REVISION_CONFLICT:${input.id}`);
    }
    const now = new Date().toISOString();
    const revision = current ? current.revision + 1 : 1;
    this.db.prepare(`
      INSERT INTO config_resources (
        id, workspace_id, user_id, kind, name, description, payload_json, secret_ref,
        default_enabled, builtin, status, revision, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(workspace_id, user_id, kind, id) DO UPDATE SET
        name = excluded.name,
        description = excluded.description,
        payload_json = excluded.payload_json,
        secret_ref = excluded.secret_ref,
        default_enabled = excluded.default_enabled,
        status = excluded.status,
        revision = excluded.revision,
        updated_at = excluded.updated_at
    `).run(
      input.id,
      input.workspace_id,
      input.user_id,
      input.kind,
      input.name,
      input.description ?? current?.description ?? null,
      JSON.stringify(input.payload ?? current?.payload ?? {}),
      input.secret_ref === null ? null : input.secret_ref ?? current?.secret_ref ?? null,
      (input.default_enabled ?? current?.default_enabled ?? true) ? 1 : 0,
      (input.builtin ?? current?.builtin ?? false) ? 1 : 0,
      input.status ?? current?.status ?? "untested",
      revision,
      current?.created_at ?? now,
      now
    );
    return this.get(input);
  }

  /** Return one configuration resource or throw when it does not exist. */
  get(input: { id: string; workspace_id: string; user_id: string; kind: ConfigResourceKind }): ConfigResourceRecord {
    const record = this.find(input);
    if (!record) {
      throw new Error(`CONFIG_RESOURCE_NOT_FOUND:${input.kind}:${input.id}`);
    }
    return record;
  }

  /** Find one configuration resource without throwing. */
  find(input: {
    id: string;
    workspace_id: string;
    user_id: string;
    kind: ConfigResourceKind;
  }): ConfigResourceRecord | undefined {
    return mapConfigResource(this.db.prepare(`
      SELECT * FROM config_resources WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
    `).get(input.workspace_id, input.user_id, input.kind, input.id));
  }

  /** List configuration resources in one workspace scope. */
  list(input: { workspace_id: string; user_id: string; kind: ConfigResourceKind }): ConfigResourceRecord[] {
    return this.db.prepare(`
      SELECT * FROM config_resources WHERE workspace_id = ? AND user_id = ? AND kind = ? ORDER BY updated_at DESC
    `).all(input.workspace_id, input.user_id, input.kind).map(mapRequiredConfigResource);
  }

  /** Delete one configuration resource. Builtin skill packages remain protected. */
  delete(input: { id: string; workspace_id: string; user_id: string; kind: ConfigResourceKind }): void {
    const current = this.get(input);
    if (current.builtin && input.kind === "skill") {
      throw new Error(`BUILTIN_RESOURCE_READONLY:${input.id}`);
    }
    this.db.prepare(`
      DELETE FROM config_resources WHERE workspace_id = ? AND user_id = ? AND kind = ? AND id = ?
    `).run(input.workspace_id, input.user_id, input.kind, input.id);
  }
}

export class EncryptedSecretStore {
  private readonly key: Buffer | undefined;

  constructor(private readonly db: DatabaseSync, masterKey?: string) {
    this.key = masterKey ? createHash("sha256").update(masterKey).digest() : undefined;
  }

  /** Encrypt and persist a write-only credential object. */
  put(input: {
    workspace_id: string;
    user_id: string;
    owner_kind: string;
    owner_id: string;
    value: Record<string, unknown>;
    secret_ref?: string;
  }): string {
    const key = this.requireKey();
    const ref = input.secret_ref ?? `secret://${input.owner_kind}/${input.owner_id}/${randomUUID()}`;
    if (input.secret_ref) {
      const owner = this.db.prepare(`
        SELECT workspace_id, user_id, owner_kind, owner_id FROM encrypted_secrets WHERE ref = ?
      `).get(ref);
      if (
        !isRecord(owner)
        || owner.workspace_id !== input.workspace_id
        || owner.user_id !== input.user_id
        || owner.owner_kind !== input.owner_kind
        || owner.owner_id !== input.owner_id
      ) {
        throw new Error(`SECRET_OWNER_MISMATCH:${ref}`);
      }
    }
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", key, iv);
    const ciphertext = Buffer.concat([cipher.update(JSON.stringify(input.value), "utf8"), cipher.final()]);
    const now = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO encrypted_secrets (
        ref, workspace_id, user_id, owner_kind, owner_id, iv, auth_tag, ciphertext, created_at, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(ref) DO UPDATE SET
        iv = excluded.iv,
        auth_tag = excluded.auth_tag,
        ciphertext = excluded.ciphertext,
        updated_at = excluded.updated_at
    `).run(
      ref,
      input.workspace_id,
      input.user_id,
      input.owner_kind,
      input.owner_id,
      iv.toString("base64"),
      cipher.getAuthTag().toString("base64"),
      ciphertext.toString("base64"),
      now,
      now
    );
    return ref;
  }

  /** Decrypt one credential object for a trusted server-side consumer. */
  get(input: { ref: string; workspace_id: string; user_id: string }): Record<string, unknown> {
    const row = this.db.prepare(`
      SELECT * FROM encrypted_secrets WHERE ref = ? AND workspace_id = ? AND user_id = ?
    `).get(input.ref, input.workspace_id, input.user_id);
    if (!isRecord(row)) {
      throw new Error(`SECRET_NOT_FOUND:${input.ref}`);
    }
    const decipher = createDecipheriv(
      "aes-256-gcm",
      this.requireKey(),
      Buffer.from(requiredString(row, "iv"), "base64")
    );
    decipher.setAuthTag(Buffer.from(requiredString(row, "auth_tag"), "base64"));
    const plaintext = Buffer.concat([
      decipher.update(Buffer.from(requiredString(row, "ciphertext"), "base64")),
      decipher.final()
    ]).toString("utf8");
    const value: unknown = JSON.parse(plaintext);
    if (!isRecord(value)) {
      throw new Error(`SECRET_PAYLOAD_INVALID:${input.ref}`);
    }
    return value;
  }

  /** Delete a secret owned by the current workspace and user. */
  delete(input: { ref: string; workspace_id: string; user_id: string }): void {
    this.db.prepare("DELETE FROM encrypted_secrets WHERE ref = ? AND workspace_id = ? AND user_id = ?")
      .run(input.ref, input.workspace_id, input.user_id);
  }

  private requireKey(): Buffer {
    if (!this.key) {
      throw new Error("SECRET_MASTER_KEY_REQUIRED");
    }
    return this.key;
  }
}

export class ConfigJobRepository {
  constructor(private readonly db: DatabaseSync) {}

  /** Create a queued persistent configuration job. */
  create(input: {
    workspace_id: string;
    user_id: string;
    type: string;
    resource_id?: string;
    idempotency_key?: string;
  }): JobRecord {
    if (input.idempotency_key) {
      const existing = this.findByIdempotency({
        workspace_id: input.workspace_id,
        user_id: input.user_id,
        type: input.type,
        idempotency_key: input.idempotency_key
      });
      if (existing) {
        return existing;
      }
    }
    const id = randomUUID();
    const createdAt = new Date().toISOString();
    this.db.prepare(`
      INSERT INTO config_jobs (
        id, workspace_id, user_id, type, resource_id, status, progress, idempotency_key, created_at
      ) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?)
    `).run(
      id,
      input.workspace_id,
      input.user_id,
      input.type,
      input.resource_id ?? null,
      input.idempotency_key ?? null,
      createdAt
    );
    return this.get({ id, workspace_id: input.workspace_id, user_id: input.user_id });
  }

  /** Return one job in the current workspace scope. */
  get(input: { id: string; workspace_id: string; user_id: string }): JobRecord {
    const record = mapJob(this.db.prepare(`
      SELECT * FROM config_jobs WHERE id = ? AND workspace_id = ? AND user_id = ?
    `).get(input.id, input.workspace_id, input.user_id));
    if (!record) {
      throw new Error(`JOB_NOT_FOUND:${input.id}`);
    }
    return record;
  }

  /** Transition a job and persist progress, result, or error. */
  update(input: {
    id: string;
    workspace_id: string;
    user_id: string;
    status: JobStatus;
    progress?: number;
    result?: unknown;
    error?: unknown;
  }): JobRecord {
    const current = this.get(input);
    const now = new Date().toISOString();
    const startedAt = input.status === "running" ? current.started_at ?? now : current.started_at;
    const finishedAt = ["completed", "failed", "canceled", "interrupted"].includes(input.status) ? now : undefined;
    this.db.prepare(`
      UPDATE config_jobs SET status = ?, progress = ?, result_json = ?, error_json = ?, started_at = ?, finished_at = ?
      WHERE id = ? AND workspace_id = ? AND user_id = ?
    `).run(
      input.status,
      Math.max(0, Math.min(100, input.progress ?? current.progress)),
      input.result === undefined ? null : JSON.stringify(input.result),
      input.error === undefined ? null : JSON.stringify(input.error),
      startedAt ?? null,
      finishedAt ?? null,
      input.id,
      input.workspace_id,
      input.user_id
    );
    return this.get(input);
  }

  private findByIdempotency(input: {
    workspace_id: string;
    user_id: string;
    type: string;
    idempotency_key: string;
  }): JobRecord | undefined {
    return mapJob(this.db.prepare(`
      SELECT * FROM config_jobs
      WHERE workspace_id = ? AND user_id = ? AND type = ? AND idempotency_key = ?
    `).get(input.workspace_id, input.user_id, input.type, input.idempotency_key));
  }
}

const mapConfigResource = (row: unknown): ConfigResourceRecord | undefined => {
  if (!isRecord(row)) {
    return undefined;
  }
  const payload: unknown = JSON.parse(requiredString(row, "payload_json"));
  if (!isRecord(payload)) {
    throw new Error("CONFIG_RESOURCE_PAYLOAD_INVALID");
  }
  const description = optionalString(row.description);
  const secretRef = optionalString(row.secret_ref);
  return {
    id: requiredString(row, "id"),
    workspace_id: requiredString(row, "workspace_id"),
    user_id: requiredString(row, "user_id"),
    kind: requiredString(row, "kind") as ConfigResourceKind,
    name: requiredString(row, "name"),
    ...(description ? { description } : {}),
    payload,
    ...(secretRef ? { secret_ref: secretRef } : {}),
    default_enabled: requiredNumber(row, "default_enabled") === 1,
    builtin: requiredNumber(row, "builtin") === 1,
    status: requiredString(row, "status"),
    revision: requiredNumber(row, "revision"),
    created_at: requiredString(row, "created_at"),
    updated_at: requiredString(row, "updated_at")
  };
};

const mapRequiredConfigResource = (row: unknown): ConfigResourceRecord => {
  const record = mapConfigResource(row);
  if (!record) {
    throw new Error("CONFIG_RESOURCE_ROW_INVALID");
  }
  return record;
};

const mapJob = (row: unknown): JobRecord | undefined => {
  if (!isRecord(row)) {
    return undefined;
  }
  const resourceId = optionalString(row.resource_id);
  const resultJson = optionalString(row.result_json);
  const errorJson = optionalString(row.error_json);
  const idempotencyKey = optionalString(row.idempotency_key);
  const startedAt = optionalString(row.started_at);
  const finishedAt = optionalString(row.finished_at);
  return {
    id: requiredString(row, "id"),
    workspace_id: requiredString(row, "workspace_id"),
    user_id: requiredString(row, "user_id"),
    type: requiredString(row, "type"),
    ...(resourceId ? { resource_id: resourceId } : {}),
    status: requiredString(row, "status") as JobStatus,
    progress: requiredNumber(row, "progress"),
    ...(resultJson ? { result: JSON.parse(resultJson) as unknown } : {}),
    ...(errorJson ? { error: JSON.parse(errorJson) as unknown } : {}),
    ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {}),
    created_at: requiredString(row, "created_at"),
    ...(startedAt ? { started_at: startedAt } : {}),
    ...(finishedAt ? { finished_at: finishedAt } : {})
  };
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
const optionalString = (value: unknown): string | undefined => typeof value === "string" ? value : undefined;

const requiredString = (row: Record<string, unknown>, key: string): string => {
  const value = row[key];
  if (typeof value !== "string") {
    throw new Error(`EXPECTED_STRING_COLUMN:${key}`);
  }
  return value;
};

const requiredNumber = (row: Record<string, unknown>, key: string): number => {
  const value = row[key];
  if (typeof value !== "number") {
    throw new Error(`EXPECTED_NUMBER_COLUMN:${key}`);
  }
  return value;
};
