import { installConsoleLogging } from "../../scripts/lib/logging.mjs";

installConsoleLogging({ file: "web", component: "web" });
await import("next/dist/bin/next");
