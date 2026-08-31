import path from "node:path";
import type { NextConfig } from "next";

const workspaceRoot = path.join(__dirname, "../..");

const nextConfig: NextConfig = {
  outputFileTracingRoot: workspaceRoot,
  // Next's default `compress: true` applies gzip to `text/*`, including
  // `text/event-stream`. Even with flush hooks, compression is the wrong layer
  // for AG-UI SSE. Disable here; terminate TLS/gzip at the reverse proxy for
  // HTML/assets (see deploy/nginx.datafoundry.conf.example), and leave
  // `/api/copilotkit` uncompressed.
  compress: false,
  // Production / test builds: tree-shake heavy package entrypoints.
  experimental: {
    optimizePackageImports: ["zod"],
  },
  // Dev uses Turbopack (see `dev` script). Declaring this key pins the
  // monorepo root and silences the "Webpack is configured while Turbopack is
  // not" warning; the webpack() hook below still applies to `next build`.
  turbopack: {
    root: workspaceRoot,
  },
  // Same-origin `/api/*` is owned by App Router route handlers
  // (`app/api/**/route.ts` → `proxyToApi`). Do not add rewrites for those
  // paths: rewrites cannot set SSE anti-buffering headers, and would race the
  // intentional streaming BFF.
  webpack(config, { isServer }) {
    if (isServer && config.output) {
      config.output.chunkFilename = "chunks/[name].js";
    }
    return config;
  },
};

export default nextConfig;
