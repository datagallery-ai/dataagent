import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { isLoopbackHostname, validateRuntimeUrl } from "./runtime-url.js";

describe("validateRuntimeUrl", () => {
  it("allows loopback HTTP and normalizes path/query/hash", () => {
    const result = validateRuntimeUrl(
      "http://127.0.0.1:8787/api/copilotkit/?x=1#frag",
    );
    assert.deepEqual(result, {
      ok: true,
      url: "http://127.0.0.1:8787/api/copilotkit",
    });
  });

  it("allows localhost HTTP and remote HTTPS", () => {
    assert.equal(validateRuntimeUrl("http://localhost:8787/api/copilotkit").ok, true);
    assert.equal(
      validateRuntimeUrl("https://api.example.com/deploy/api/copilotkit/").ok,
      true,
    );
    assert.equal(
      (validateRuntimeUrl("https://api.example.com/deploy/api/copilotkit/") as { url: string }).url,
      "https://api.example.com/deploy/api/copilotkit",
    );
  });

  it("rejects non-loopback plaintext HTTP", () => {
    const result = validateRuntimeUrl("http://api.example.com/api/copilotkit");
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.match(result.reason, /HTTPS/i);
    }
  });

  it("rejects URL credentials", () => {
    const result = validateRuntimeUrl("https://user:pass@example.com/api/copilotkit");
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.match(result.reason, /credentials/i);
    }
  });

  it("rejects non-http(s) schemes", () => {
    const result = validateRuntimeUrl("ftp://127.0.0.1/api/copilotkit");
    assert.equal(result.ok, false);
  });
});

describe("isLoopbackHostname", () => {
  it("recognizes common loopback forms", () => {
    assert.equal(isLoopbackHostname("localhost"), true);
    assert.equal(isLoopbackHostname("127.0.0.1"), true);
    assert.equal(isLoopbackHostname("127.1.2.3"), true);
    assert.equal(isLoopbackHostname("::1"), true);
    assert.equal(isLoopbackHostname("[::1]"), true);
    assert.equal(isLoopbackHostname("example.com"), false);
    assert.equal(isLoopbackHostname("192.168.0.1"), false);
  });
});
