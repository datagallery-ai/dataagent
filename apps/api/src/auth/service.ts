import { randomUUID } from "node:crypto";
import type {
  AuthSessionRecord,
  MetadataStore,
  UserRecord,
  WorkspaceRecord
} from "@datafoundry/metadata";

import type { PasswordAuthConfig } from "./config.js";
import { AuthMailer } from "./mailer.js";
import { createSecretToken, hashPassword, hashToken, verifyPassword } from "./crypto.js";

const WEB_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60;
const TUI_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60;
const EMAIL_VERIFICATION_TTL_MS = 1000 * 60 * 60 * 24;
const PASSWORD_RESET_TTL_MS = 1000 * 60 * 30;

export type AuthClientKind = "web" | "tui";

export type AuthUserDto = {
  id: string;
  email?: string;
  displayName?: string;
};

export type AuthWorkspaceDto = {
  id: string;
  name?: string;
};

export type AuthIdentity = {
  session?: AuthSessionRecord;
  user: UserRecord;
  workspace: WorkspaceRecord;
};

export class AuthError extends Error {
  constructor(readonly status: number, readonly code: string, message: string) {
    super(message);
    this.name = "AuthError";
  }
}

export class AuthService {
  private readonly mailer: AuthMailer;
  private readonly dummyPasswordHash = hashPassword(createSecretToken()).then((result) => result.hash);

  constructor(
    private readonly metadataStore: MetadataStore,
    private readonly config: PasswordAuthConfig
  ) {
    this.mailer = new AuthMailer(config);
  }

  getPublicStatus(): { publicBaseUrl: string; registrationEnabled: boolean } {
    return {
      publicBaseUrl: this.config.publicBaseUrl,
      registrationEnabled: this.config.registrationMode === "open"
    };
  }

  async register(input: {
    displayName?: string | undefined;
    email: string;
    ipAddress?: string | undefined;
    password: string;
    userAgent?: string | undefined;
  }): Promise<{ user: AuthUserDto; workspace: AuthWorkspaceDto; verificationToken?: string }> {
    if (this.config.registrationMode !== "open") {
      throw new AuthError(403, "REGISTRATION_CLOSED", "Registration is closed for this deployment.");
    }
    const email = normalizeEmail(input.email);
    assertPassword(input.password);
    this.checkRateLimit(`register:ip:${input.ipAddress ?? "unknown"}`, 5, 60 * 60);
    if (this.metadataStore.users.findByEmail({ email })) {
      throw new AuthError(409, "CONFLICT", "Email is already registered.");
    }
    const user = this.metadataStore.users.createPasswordUser({
      id: randomUUID(),
      email,
      ...(input.displayName ? { display_name: normalizeDisplayName(input.displayName) } : {})
    });
    const password = await hashPassword(input.password);
    this.metadataStore.userPasswordCredentials.set({
      user_id: user.id,
      password_hash: password.hash,
      password_hash_params: password.params
    });
    this.metadataStore.users.touchPasswordUpdated({ user_id: user.id });
    const workspace = this.ensurePersonalWorkspace(user);
    const verificationToken = createSecretToken();
    this.metadataStore.authTokens.create({
      id: randomUUID(),
      user_id: user.id,
      purpose: "email_verification",
      token_hash: hashToken(verificationToken, this.config.sessionSecret),
      expires_at: new Date(Date.now() + EMAIL_VERIFICATION_TTL_MS).toISOString()
    });
    const mail = await this.mailer.sendVerification({ email, token: verificationToken });
    this.audit("auth.register", { email, ipAddress: input.ipAddress, userAgent: input.userAgent, userId: user.id });
    return {
      user: userDto(user),
      workspace: workspaceDto(workspace),
      ...(mail.testToken ? { verificationToken: mail.testToken } : {})
    };
  }

  async verifyEmail(input: {
    token: string;
    ipAddress?: string | undefined;
    userAgent?: string | undefined;
  }): Promise<{ user: AuthUserDto }> {
    const token = this.requireToken("email_verification", input.token);
    const user = this.metadataStore.users.markEmailVerified({ user_id: token.user_id });
    this.metadataStore.authTokens.consume({ id: token.id });
    this.audit("auth.email_verified", {
      email: user.email,
      ipAddress: input.ipAddress,
      userAgent: input.userAgent,
      userId: user.id
    });
    return { user: userDto(user) };
  }

  async login(input: {
    client?: AuthClientKind | undefined;
    email: string;
    ipAddress?: string | undefined;
    password: string;
    userAgent?: string | undefined;
  }): Promise<{
    csrfToken: string;
    expiresAt: string;
    maxAgeSeconds: number;
    sessionToken: string;
    user: AuthUserDto;
    workspace: AuthWorkspaceDto;
  }> {
    const email = normalizeEmail(input.email);
    this.checkRateLimit(`login:email:${email}`, 5, 60);
    this.checkRateLimit(`login:ip:${input.ipAddress ?? "unknown"}`, 20, 60);
    const user = this.metadataStore.users.findByEmail({ email });
    const credential = user
      ? this.metadataStore.userPasswordCredentials.find({ user_id: user.id })
      : undefined;
    if (!user || user.disabled_at || !credential) {
      await verifyPassword(await this.dummyPasswordHash, input.password);
      this.audit("auth.login_failed", { email, ipAddress: input.ipAddress, userAgent: input.userAgent });
      throw new AuthError(401, "UNAUTHORIZED", "Invalid email or password.");
    }
    if (!(await verifyPassword(credential.password_hash, input.password))) {
      this.audit("auth.login_failed", {
        email,
        ipAddress: input.ipAddress,
        userAgent: input.userAgent,
        userId: user.id
      });
      throw new AuthError(401, "UNAUTHORIZED", "Invalid email or password.");
    }
    if (!user.email_verified_at) {
      this.audit("auth.login_unverified", {
        email,
        ipAddress: input.ipAddress,
        userAgent: input.userAgent,
        userId: user.id
      });
      throw new AuthError(403, "EMAIL_NOT_VERIFIED", "Email verification is required before login.");
    }
    const maxAgeSeconds = input.client === "tui" ? TUI_SESSION_TTL_SECONDS : WEB_SESSION_TTL_SECONDS;
    const expiresAt = new Date(Date.now() + maxAgeSeconds * 1000).toISOString();
    const sessionToken = createSecretToken();
    const csrfToken = createSecretToken();
    const session = this.metadataStore.authSessions.create({
      id: randomUUID(),
      user_id: user.id,
      token_hash: hashToken(sessionToken, this.config.sessionSecret),
      csrf_token_hash: hashToken(csrfToken, this.config.sessionSecret),
      expires_at: expiresAt,
      ...(input.ipAddress ? { ip_address: input.ipAddress } : {}),
      ...(input.userAgent ? { user_agent: input.userAgent } : {})
    });
    const workspace = this.ensurePersonalWorkspace(user);
    this.audit("auth.login_succeeded", {
      email,
      ipAddress: input.ipAddress,
      metadata: { sessionId: session.id, client: input.client ?? "web" },
      userAgent: input.userAgent,
      userId: user.id
    });
    return {
      csrfToken,
      expiresAt: session.expires_at,
      maxAgeSeconds,
      sessionToken,
      user: userDto(user),
      workspace: workspaceDto(workspace)
    };
  }

  async forgotPassword(input: {
    email: string;
    ipAddress?: string | undefined;
    userAgent?: string | undefined;
  }): Promise<{ resetToken?: string; ok: boolean }> {
    const email = normalizeEmail(input.email);
    this.checkRateLimit(`password-reset:email:${email}`, 3, 60 * 60);
    const user = this.metadataStore.users.findByEmail({ email });
    if (!user) {
      this.audit("auth.password_reset_requested_unknown", {
        email,
        ipAddress: input.ipAddress,
        userAgent: input.userAgent
      });
      return { ok: true };
    }
    const resetToken = createSecretToken();
    this.metadataStore.authTokens.create({
      id: randomUUID(),
      user_id: user.id,
      purpose: "password_reset",
      token_hash: hashToken(resetToken, this.config.sessionSecret),
      expires_at: new Date(Date.now() + PASSWORD_RESET_TTL_MS).toISOString()
    });
    const mail = await this.mailer.sendPasswordReset({ email, token: resetToken });
    this.audit("auth.password_reset_requested", {
      email,
      ipAddress: input.ipAddress,
      userAgent: input.userAgent,
      userId: user.id
    });
    return { ok: true, ...(mail.testToken ? { resetToken: mail.testToken } : {}) };
  }

  async resetPassword(input: {
    ipAddress?: string | undefined;
    password: string;
    token: string;
    userAgent?: string | undefined;
  }): Promise<{ ok: boolean }> {
    assertPassword(input.password);
    const token = this.requireToken("password_reset", input.token);
    const password = await hashPassword(input.password);
    this.metadataStore.userPasswordCredentials.set({
      user_id: token.user_id,
      password_hash: password.hash,
      password_hash_params: password.params
    });
    const user = this.metadataStore.users.touchPasswordUpdated({ user_id: token.user_id });
    this.metadataStore.authTokens.consume({ id: token.id });
    this.metadataStore.authSessions.revokeByUser({ user_id: token.user_id });
    this.audit("auth.password_reset_completed", {
      email: user.email,
      ipAddress: input.ipAddress,
      userAgent: input.userAgent,
      userId: user.id
    });
    return { ok: true };
  }

  async changePassword(input: {
    currentPassword: string;
    identity: AuthIdentity;
    ipAddress?: string | undefined;
    newPassword: string;
    userAgent?: string | undefined;
  }): Promise<{ ok: boolean }> {
    assertPassword(input.newPassword);
    const credential = this.metadataStore.userPasswordCredentials.get({ user_id: input.identity.user.id });
    if (!(await verifyPassword(credential.password_hash, input.currentPassword))) {
      throw new AuthError(401, "UNAUTHORIZED", "Current password is invalid.");
    }
    const password = await hashPassword(input.newPassword);
    this.metadataStore.userPasswordCredentials.set({
      user_id: input.identity.user.id,
      password_hash: password.hash,
      password_hash_params: password.params
    });
    this.metadataStore.users.touchPasswordUpdated({ user_id: input.identity.user.id });
    this.metadataStore.authSessions.revokeByUser({
      user_id: input.identity.user.id,
      ...(input.identity.session ? { except_session_id: input.identity.session.id } : {})
    });
    this.audit("auth.password_changed", {
      email: input.identity.user.email,
      ipAddress: input.ipAddress,
      userAgent: input.userAgent,
      userId: input.identity.user.id
    });
    return { ok: true };
  }

  authenticateSession(sessionToken: string | undefined): AuthIdentity {
    if (!sessionToken) {
      throw new AuthError(401, "UNAUTHORIZED", "Authentication required.");
    }
    const session = this.metadataStore.authSessions.findByTokenHash({
      token_hash: hashToken(sessionToken, this.config.sessionSecret)
    });
    if (!session) {
      throw new AuthError(401, "UNAUTHORIZED", "Authentication required.");
    }
    const user = this.metadataStore.users.getById({ user_id: session.user_id });
    if (user.disabled_at) {
      throw new AuthError(401, "UNAUTHORIZED", "Authentication required.");
    }
    this.metadataStore.authSessions.touch({ id: session.id });
    return {
      session,
      user,
      workspace: this.ensurePersonalWorkspace(user)
    };
  }

  validateCsrf(identity: AuthIdentity, csrfToken: string | undefined): void {
    if (!identity.session || !csrfToken) {
      throw new AuthError(403, "CSRF_INVALID", "CSRF token is required.");
    }
    const tokenHash = hashToken(csrfToken, this.config.sessionSecret);
    if (tokenHash !== identity.session.csrf_token_hash) {
      throw new AuthError(403, "CSRF_INVALID", "CSRF token is invalid.");
    }
  }

  rotateCsrf(identity: AuthIdentity): { csrfToken: string; maxAgeSeconds: number } {
    if (!identity.session) {
      throw new AuthError(401, "UNAUTHORIZED", "Authentication required.");
    }
    const csrfToken = createSecretToken();
    try {
      this.metadataStore.authSessions.rotateCsrf({
        id: identity.session.id,
        csrf_token_hash: hashToken(csrfToken, this.config.sessionSecret)
      });
    } catch (error) {
      if (error instanceof Error && error.message.startsWith("AUTH_SESSION_CSRF_ROTATE_FAILED:")) {
        throw new AuthError(401, "UNAUTHORIZED", "Authentication required.");
      }
      throw error;
    }
    const remainingMs = Date.parse(identity.session.expires_at) - Date.now();
    const maxAgeSeconds = Math.max(0, Math.floor(remainingMs / 1000));
    return { csrfToken, maxAgeSeconds };
  }

  logout(identity: AuthIdentity): { ok: boolean } {
    if (identity.session) {
      this.metadataStore.authSessions.revoke({ id: identity.session.id });
      this.audit("auth.logout", { email: identity.user.email, userId: identity.user.id });
    }
    return { ok: true };
  }

  logoutAll(identity: AuthIdentity): { ok: boolean } {
    this.metadataStore.authSessions.revokeByUser({ user_id: identity.user.id });
    this.audit("auth.logout_all", { email: identity.user.email, userId: identity.user.id });
    return { ok: true };
  }

  listSessions(identity: AuthIdentity): { sessions: Array<Record<string, unknown>> } {
    return {
      sessions: this.metadataStore.authSessions.listByUser({ user_id: identity.user.id }).map((session) => ({
        id: session.id,
        createdAt: session.created_at,
        expiresAt: session.expires_at,
        lastSeenAt: session.last_seen_at,
        userAgent: session.user_agent
      }))
    };
  }

  revokeSession(identity: AuthIdentity, id: string): { revoked: boolean } {
    const sessions = this.metadataStore.authSessions.listByUser({ user_id: identity.user.id });
    if (!sessions.some((session) => session.id === id)) {
      throw new AuthError(404, "RESOURCE_NOT_FOUND", "Session not found.");
    }
    this.metadataStore.authSessions.revoke({ id });
    return { revoked: true };
  }

  private requireToken(purpose: "email_verification" | "password_reset", token: string) {
    const record = this.metadataStore.authTokens.findValid({
      purpose,
      token_hash: hashToken(token, this.config.sessionSecret)
    });
    if (!record) {
      throw new AuthError(400, "BAD_REQUEST", "Token is invalid or expired.");
    }
    return record;
  }

  private ensurePersonalWorkspace(user: UserRecord): WorkspaceRecord {
    const existing = this.metadataStore.workspaces.findPersonalByUser({ user_id: user.id });
    if (existing) {
      this.metadataStore.workspaceMemberships.upsertOwner({ workspace_id: existing.id, user_id: user.id });
      return existing;
    }
    const workspace = this.metadataStore.workspaces.createPersonal({
      id: `personal-${user.id}`,
      owner_user_id: user.id,
      name: user.display_name ? `${user.display_name}'s workspace` : "Personal workspace"
    });
    this.metadataStore.workspaceMemberships.upsertOwner({ workspace_id: workspace.id, user_id: user.id });
    return workspace;
  }

  private checkRateLimit(bucket: string, limit: number, windowSeconds: number): void {
    const now = new Date();
    const current = this.metadataStore.db.prepare(`
      SELECT bucket, count, reset_at FROM auth_rate_limits WHERE bucket = ?
    `).get(bucket);
    if (isRateLimitRow(current) && new Date(String(current.reset_at)).getTime() > now.getTime()) {
      if (Number(current.count) >= limit) {
        throw new AuthError(429, "RATE_LIMITED", "Too many requests. Try again later.");
      }
      this.metadataStore.db.prepare(`
        UPDATE auth_rate_limits SET count = count + 1, updated_at = ? WHERE bucket = ?
      `).run(now.toISOString(), bucket);
      return;
    }
    this.metadataStore.db.prepare(`
      INSERT INTO auth_rate_limits (bucket, count, reset_at, updated_at)
      VALUES (?, 1, ?, ?)
      ON CONFLICT(bucket) DO UPDATE SET count = 1, reset_at = excluded.reset_at, updated_at = excluded.updated_at
    `).run(bucket, new Date(now.getTime() + windowSeconds * 1000).toISOString(), now.toISOString());
  }

  private audit(
    eventType: string,
    input: {
      email?: string | undefined;
      ipAddress?: string | undefined;
      metadata?: unknown;
      userAgent?: string | undefined;
      userId?: string | undefined;
    }
  ): void {
    this.metadataStore.authAuditEvents.append({
      id: randomUUID(),
      event_type: eventType,
      ...(input.email ? { email: input.email } : {}),
      ...(input.ipAddress ? { ip_address: input.ipAddress } : {}),
      ...(input.metadata ? { metadata: input.metadata } : {}),
      ...(input.userAgent ? { user_agent: input.userAgent } : {}),
      ...(input.userId ? { user_id: input.userId } : {})
    });
  }
}

export function userDto(user: UserRecord): AuthUserDto {
  return {
    id: user.id,
    ...(user.email ? { email: user.email } : {}),
    ...(user.display_name ? { displayName: user.display_name } : {})
  };
}

export function workspaceDto(workspace: WorkspaceRecord): AuthWorkspaceDto {
  return {
    id: workspace.id,
    name: workspace.name
  };
}

function normalizeEmail(email: string): string {
  const normalized = email.trim().toLowerCase();
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/u.test(normalized)) {
    throw new AuthError(400, "BAD_REQUEST", "A valid email address is required.");
  }
  return normalized;
}

function normalizeDisplayName(value: string): string {
  const normalized = value.replace(/\s+/gu, " ").trim();
  return normalized.slice(0, 80);
}

function assertPassword(password: string): void {
  if (password.length < 6) {
    throw new AuthError(400, "BAD_REQUEST", "Password must be at least 6 characters.");
  }
}

function isRateLimitRow(value: unknown): value is { count: number; reset_at: string } {
  return typeof value === "object" && value !== null && "count" in value && "reset_at" in value;
}
