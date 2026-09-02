#!/usr/bin/env node
import { createRuntimeStubServer } from "../apps/api/dist/runtime/stub-server.js";

const host = process.env.RUNTIME_STUB_HOST ?? "127.0.0.1";
const port = Number.parseInt(process.env.RUNTIME_STUB_PORT ?? "8790", 10);
const server = createRuntimeStubServer({
  ...(process.env.RUNTIME_SERVICE_TOKEN ? { token: process.env.RUNTIME_SERVICE_TOKEN } : {})
});

server.listen(port, host, () => {
  console.log(`[runtime-stub] listening on http://${host}:${port}`);
});
