import assert from "node:assert/strict";
import test from "node:test";
import { deploymentHelp, parseDeployArgs } from "./args.mjs";

test("defaults to deploy", () => {
  assert.deepEqual(parseDeployArgs([]), {
    command: "deploy",
    reconfigure: false,
    nonInteractive: false,
    runtimeUrl: null
  });
});

test("accepts flags before or after deploy", () => {
  assert.deepEqual(parseDeployArgs(["--non-interactive"]), {
    command: "deploy",
    reconfigure: false,
    nonInteractive: true,
    runtimeUrl: null
  });
  assert.deepEqual(parseDeployArgs(["deploy", "--reconfigure"]), {
    command: "deploy",
    reconfigure: true,
    nonInteractive: false,
    runtimeUrl: null
  });
  assert.deepEqual(parseDeployArgs(["--reconfigure", "deploy"]), {
    command: "deploy",
    reconfigure: true,
    nonInteractive: false,
    runtimeUrl: null
  });
});

test("supports every lifecycle command", () => {
  for (const command of ["start", "stop", "restart", "status", "logs", "doctor", "tui", "help"]) {
    assert.equal(parseDeployArgs([command]).command, command);
  }
});

test("tui accepts optional --runtime-url", () => {
  assert.deepEqual(parseDeployArgs(["tui"]), {
    command: "tui",
    reconfigure: false,
    nonInteractive: false,
    runtimeUrl: null
  });
  assert.deepEqual(parseDeployArgs(["tui", "--runtime-url", "http://127.0.0.1:9000/api/copilotkit"]), {
    command: "tui",
    reconfigure: false,
    nonInteractive: false,
    runtimeUrl: "http://127.0.0.1:9000/api/copilotkit"
  });
  assert.deepEqual(parseDeployArgs(["--runtime-url", "http://example/api/copilotkit", "tui"]), {
    command: "tui",
    reconfigure: false,
    nonInteractive: false,
    runtimeUrl: "http://example/api/copilotkit"
  });
});

test("rejects unknown commands and invalid flag combinations", () => {
  assert.throws(() => parseDeployArgs(["start", "--reconfigure"]), /--reconfigure is only valid with deploy/);
  assert.throws(() => parseDeployArgs(["status", "--non-interactive"]), /--non-interactive is only valid with deploy/);
  assert.throws(
    () => parseDeployArgs(["deploy", "--reconfigure", "--non-interactive"]),
    /mutually exclusive/
  );
  assert.throws(() => parseDeployArgs(["deploy", "--runtime-url", "http://x"]), /--runtime-url is only valid with tui/);
  assert.throws(() => parseDeployArgs(["tui", "--runtime-url"]), /--runtime-url requires a URL value/);
  assert.throws(() => parseDeployArgs(["wat"]), /Unknown command/);
});

test("help lists lifecycle commands including tui and build readiness note", () => {
  const help = deploymentHelp();
  for (const command of ["deploy", "start", "stop", "restart", "status", "logs", "doctor", "tui", "help"]) {
    assert.match(help, new RegExp(`^${command}\\b`, "m"));
  }
  assert.match(help, /builds the TUI|build \(including TUI\)/i);
  assert.match(help, /\.\/deploy\.sh tui/);
  assert.match(help, /does not stay running/i);
});
