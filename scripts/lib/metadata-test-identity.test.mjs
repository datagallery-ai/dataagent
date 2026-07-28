import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createMetadataStore } from "../../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "./metadata-test-identity.mjs";

test("createVerifiedTestIdentity returns unique verified users without credentials", () => {
  const root = mkdtempSync(join(tmpdir(), "metadata-test-identity-"));
  try {
    const metadata = createMetadataStore({ database_path: join(root, "a.sqlite") });
    const first = createVerifiedTestIdentity(metadata);
    const second = createVerifiedTestIdentity(metadata);

    assert.notEqual(first.userId, second.userId);
    assert.notEqual(first.workspaceId, second.workspaceId);
    assert.notEqual(first.email, second.email);

    const user = metadata.users.getById({ user_id: first.userId });
    assert.ok(user.email_verified_at);

    const credential = metadata.userPasswordCredentials.find({ user_id: first.userId });
    assert.equal(credential, undefined);

    const membership = metadata.workspaceMemberships.get({
      workspace_id: first.workspaceId,
      user_id: first.userId,
    });
    assert.ok(membership);
    assert.equal(membership.role, "owner");

    metadata.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("fresh metadata schema has no default user and no dev_token column", () => {
  const root = mkdtempSync(join(tmpdir(), "metadata-schema-"));
  try {
    const metadata = createMetadataStore({ database_path: join(root, "fresh.sqlite") });
    const columns = metadata.db.prepare("PRAGMA table_info(users)").all().map((row) => row.name);
    assert.ok(!columns.includes("dev_token"));
    assert.equal(metadata.users.list().length, 0);
    metadata.close();
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
