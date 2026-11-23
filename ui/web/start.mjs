import { installConsoleLogging } from "../shared/logging.mjs";

installConsoleLogging({ file: "web", component: "web" });
await import("next/dist/bin/next");
