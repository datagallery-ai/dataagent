/**
 * Unit checks for knowledge / skill whitelist fixes:
 * - decodeMultipartFilename (UTF-8 via Latin-1 round-trip)
 * - selectSkillsForRun prefers activeSkillId under maxSkills truncation
 * - extractSkillPolicy default maxSkills is 20
 */
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { decodeMultipartFilename } from "../apps/api/dist/upload-parser.js";
import { extractEffectiveRunConfig } from "../apps/api/dist/run-input.js";
import {
  buildSkillResourcePayload,
  parseSkillPackage,
  selectSkillsForRun
} from "../packages/skills/dist/index.js";
import { LocalFileAssetService } from "../packages/files/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

test("decodeMultipartFilename recovers UTF-8 Chinese names misread as Latin-1", () => {
  const original = "张三-2024-ProAgent.pdf";
  const mojibake = Buffer.from(original, "utf8").toString("latin1");
  assert.notEqual(mojibake, original);
  assert.equal(decodeMultipartFilename(mojibake), original);
  assert.equal(decodeMultipartFilename(original), original);
  assert.equal(decodeMultipartFilename("ascii-name.csv"), "ascii-name.csv");
});

test("extractEffectiveRunConfig defaults maxSkills to 20", () => {
  const config = extractEffectiveRunConfig({
    threadId: "t",
    runId: "r",
    messages: [],
    tools: [],
    context: [],
    state: {},
    forwardedProps: {}
  });
  assert.equal(config.skillPolicy.maxSkills, 20);
});

test("selectSkillsForRun keeps activeSkillId when maxSkills truncates", async () => {
  const root = mkdtempSync(join(tmpdir(), "skill-active-truncate-"));
  const metadataStore = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
const __testIdentity = createVerifiedTestIdentity(metadataStore);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

  const fileAssetService = new LocalFileAssetService(metadataStore, { storageRoot: join(root, "files") });
  

  const skillIds = [];
  for (let index = 0; index < 8; index += 1) {
    const id = `skill-${String.fromCharCode(97 + index)}`;
    skillIds.push(id);
    const body = Buffer.from(`---
name: ${id}
description: Skill ${id} for truncation test.
version: 1.0.0
allowed-tools:
  - list_files
user-invocable: true
---
Instructions for ${id}.
`, "utf8");
    const parsed = await parseSkillPackage({
      content: body,
      filename: "SKILL.md",
      mimeType: "text/markdown"
    });
    const packageRef = fileAssetService.createRef({
      user_id: userId,
      workspace_id: workspaceId,
      filename: "SKILL.md",
      content: body,
      declared_mime_type: "text/markdown",
      source: "skill-package",
      metadata: { kind: "skill-package" }
    });
    metadataStore.configResources.upsert({
      id,
      workspace_id: workspaceId,
      user_id: userId,
      kind: "skill",
      name: parsed.name,
      description: parsed.description,
      payload: buildSkillResourcePayload({
        packageFileRefId: packageRef.ref.id,
        parsed
      }),
      default_enabled: true,
      status: "valid"
    });
  }

  const activeSkillId = "skill-h";
  const selection = selectSkillsForRun({
    metadataStore,
    userId,
    workspaceId,
    userInput: "hello",
    runConfig: {
      activeSkillId,
      enabledSkillIds: skillIds,
      skillIds: [],
      skillMode: "selected",
      skillPolicy: {
        deniedToolNames: [],
        maxSkills: 3,
        requireUserInvocable: true,
        strictSkillTools: false
      },
      skillTags: []
    }
  });

  assert.equal(selection.selectedSkills.length, 3);
  assert.ok(
    selection.selectedSkills.some((skill) => skill.id === activeSkillId),
    `expected active skill ${activeSkillId} to survive truncation, got ${selection.selectedSkills.map((s) => s.id).join(",")}`
  );
});
