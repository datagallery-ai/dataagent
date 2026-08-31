import assert from "node:assert/strict";
import test from "node:test";
import {
  isRevisionConflictError,
  modelProfileTestFailureMessage,
  modelProfileTestSuccessReason
} from "../apps/api/src/model-profile-test.ts";

test("modelProfileTestFailureMessage keeps PROVIDER_CONFIG_MISSING", () => {
  assert.equal(
    modelProfileTestFailureMessage(new Error("PROVIDER_CONFIG_MISSING:server-default")),
    "PROVIDER_CONFIG_MISSING:server-default"
  );
});

test("modelProfileTestFailureMessage wraps provider probe errors", () => {
  assert.equal(
    modelProfileTestFailureMessage(new Error("Incorrect API key provided")),
    "PROVIDER_TEST_FAILED:Incorrect API key provided"
  );
});

test("modelProfileTestFailureMessage maps timeout errors", () => {
  const timeout = new Error("The operation was aborted due to timeout");
  timeout.name = "TimeoutError";
  assert.equal(
    modelProfileTestFailureMessage(timeout),
    "PROVIDER_TEST_FAILED:Connection timed out while reaching the model provider."
  );
});

test("modelProfileTestSuccessReason includes model and response", () => {
  assert.equal(
    modelProfileTestSuccessReason({ model: "qwen-plus", response: "OK" }),
    'Model "qwen-plus" responded successfully (OK).'
  );
});

test("isRevisionConflictError detects revision conflicts", () => {
  assert.equal(isRevisionConflictError(new Error("REVISION_CONFLICT:custom-1")), true);
  assert.equal(isRevisionConflictError(new Error("PROVIDER_TEST_FAILED:boom")), false);
});
