import { afterEach, describe, expect, it, vi } from "vitest";
import {
  datasourceDtoToItem,
  itemToCreateBody,
  itemToPatchBody,
  mcpServerDtoToItem,
  modelProfileDtoToItem,
  workspaceConfigDtoToStore,
} from "../../../lib/config-api/adapter";
import {
  clearConfigApiIdentity,
  configApi,
  setConfigApiIdentity,
} from "../../../lib/config-api/client";

afterEach(() => {
  vi.unstubAllGlobals();
  clearConfigApiIdentity();
  delete process.env.NEXT_PUBLIC_CONFIG_API_URL;
});

describe("config api adapter", () => {
  it("does not add development auth headers to REST requests", async () => {
    process.env.NEXT_PUBLIC_CONFIG_API_URL = "";
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      success: true,
      data: { "chat.fileUpload": true },
    }), { headers: { "Content-Type": "application/json" }, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    setConfigApiIdentity({
      userId: "tenant-user",
      displayName: "Tenant User",
      email: "tenant@example.com",
    });

    await configApi.getCapabilities();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/capabilities",
      expect.objectContaining({
        credentials: "same-origin",
        headers: expect.objectContaining({
          Accept: "application/json",
        }),
      }),
    );
    expect(fetchMock.mock.calls[0]?.[1]?.headers).not.toMatchObject({
      Authorization: expect.anything(),
    });
  });

  it("uses cookie credentials and csrf headers", async () => {
    process.env.NEXT_PUBLIC_CONFIG_API_URL = "";
    vi.stubGlobal("document", { cookie: "df_csrf=csrf-token" });
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      success: true,
      data: { deleted: true, id: "db-1" },
    }), { headers: { "Content-Type": "application/json" }, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    setConfigApiIdentity({
      userId: "tenant-user",
      displayName: "Tenant User",
      email: "tenant@example.com",
    });

    await configApi.deleteDatasource("db-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/datasources/db-1",
      expect.objectContaining({
        credentials: "same-origin",
        headers: expect.objectContaining({
          Accept: "application/json",
          "X-CSRF-Token": "csrf-token",
        }),
      }),
    );
  });

  it("maps datasource dto into workspace item settings", () => {
    const item = datasourceDtoToItem({
      id: "sales-pg",
      name: "Sales PG",
      description: "Readonly",
      type: "postgresql",
      config: {
        host: "127.0.0.1",
        port: 5432,
        database: "sales",
        queryPolicy: { maxRows: 500, timeoutMs: 5000 },
      },
      hasSecret: true,
      defaultEnabled: true,
      connectionStatus: "connected",
      revision: 2,
    });

    expect(item.settings?.host).toBe("127.0.0.1");
    expect(item.settings?.maxRows).toBe("500");
    expect(item.hasSecret).toBe(true);
    expect(item.revision).toBe(2);
  });

  it("builds create body with credentials separated from config", () => {
    const body = itemToCreateBody("db", {
      id: "sales-pg",
      name: "Sales PG",
      description: "",
      enabled: true,
      settings: {
        datasourceId: "sales-pg",
        type: "postgresql",
        host: "127.0.0.1",
        port: "5432",
        database: "sales",
        username: "readonly",
        password: "secret",
        secure: "true",
      },
    });

    expect(body.credentials).toEqual({ password: "secret" });
    expect(body.config).toMatchObject({
      host: "127.0.0.1",
      database: "sales",
      secure: true,
    });
    expect(JSON.stringify(body.config)).not.toContain("secret");
  });

  it("includes revision on patch body", () => {
    const body = itemToPatchBody("llm", {
      id: "deepseek",
      name: "DeepSeek",
      description: "",
      enabled: true,
      revision: 4,
      settings: { modelName: "deepseek-chat" },
    });

    expect(body.revision).toBe(4);
  });

  it("builds llm advanced sampling body", () => {
    const body = itemToCreateBody("llm", {
      id: "qwen-long",
      name: "Qwen Long",
      description: "",
      enabled: true,
      settings: {
        provider: "openai-compatible",
        modelName: "qwen-long",
        baseUrl: "https://example.com/v1",
        contextLength: "128000",
        reasoningModel: "true",
      },
    });

    expect(body.contextLength).toBe(128000);
    expect(body.reasoningModel).toBe(true);
  });

  it("normalizes legacy llm providers to openai-compatible", () => {
    const item = modelProfileDtoToItem({
      id: "deepseek",
      name: "DeepSeek",
      provider: "deepseek",
      modelName: "deepseek-chat",
    });
    const createBody = itemToCreateBody("llm", {
      id: "qwen",
      name: "Qwen",
      description: "",
      enabled: true,
      settings: { provider: "openai_compatible", modelName: "qwen-plus" },
    });
    const patchBody = itemToPatchBody("llm", {
      id: "qwen",
      name: "Qwen",
      description: "",
      enabled: true,
      settings: { provider: "bailian" },
    });

    expect(item.settings?.provider).toBe("openai-compatible");
    expect(createBody.provider).toBe("openai-compatible");
    expect(patchBody.provider).toBe("openai-compatible");
  });

  it("maps workspace config dto to store buckets", () => {
    const store = workspaceConfigDtoToStore({
      datasources: [],
      knowledgeBases: [
        {
          id: "metrics-docs",
          name: "Metrics",
          chunkOverlap: 64,
          chunkSize: 1200,
          citationRequired: false,
          retrievalTopK: 8,
          scoreThreshold: 0.2,
          scope: "project",
          defaultEnabled: true,
          indexStatus: "ready",
        },
      ],
      mcpServers: [],
      modelProfiles: [],
      skills: [],
    });

    expect(store.kb).toHaveLength(1);
    expect(store.kb[0]?.settings?.retrievalTopK).toBe("8");
    expect(store.kb[0]?.settings?.chunkSize).toBe("1200");
    expect(store.kb[0]?.settings?.chunkOverlap).toBe("64");
    expect(store.kb[0]?.settings?.citationRequired).toBe("false");
    expect(store.kb[0]?.settings?.scope).toBe("project");
  });

  it("builds kb create body with embedding fields", () => {
    const body = itemToCreateBody("kb", {
      id: "metrics-docs",
      name: "Metrics",
      description: "",
      enabled: true,
      settings: {
        indexName: "metrics-docs",
        retrievalTopK: "8",
        scoreThreshold: "0.2",
        chunkSize: "1200",
        chunkOverlap: "64",
        citationRequired: "true",
        scope: "workspace",
        embeddingProvider: "bailian",
        embeddingModel: "text-embedding-v4",
        embeddingBaseUrl: "https://example.com/v1",
        embeddingApiKey: "emb-secret",
      },
    });

    expect(body.embeddingProvider).toBe("bailian");
    expect(body.embeddingModel).toBe("text-embedding-v4");
    expect(body.chunkSize).toBe(1200);
    expect(body.chunkOverlap).toBe(64);
    expect(body.citationRequired).toBe(true);
    expect(body.scope).toBe("workspace");
    expect(body.credentials).toEqual({ apiKey: "emb-secret" });
  });

  it("builds mcp create body with auth type and token", () => {
    const body = itemToCreateBody("mcp", {
      id: "notion",
      name: "Notion",
      description: "",
      enabled: true,
      settings: {
        transport: "sse",
        serverUrl: "https://example.com/mcp/sse",
        authType: "bearer",
        apiKey: "token-value",
        toolAllowlist: "search, fetch_page",
        timeoutMs: "45000",
      },
    });

    expect(body.authType).toBe("bearer");
    expect(body.toolAllowlist).toEqual(["search", "fetch_page"]);
    expect(body.timeoutMs).toBe(45000);
    expect(body.credentials).toEqual({ token: "token-value" });
  });

  it("builds mcp patch body with tool policy fields", () => {
    const body = itemToPatchBody("mcp", {
      id: "notion",
      name: "Notion",
      description: "",
      enabled: true,
      settings: {
        transport: "sse",
        serverUrl: "https://example.com/mcp/sse",
        authType: "bearer",
        toolAllowlist: "search, fetch_page",
        timeoutMs: "45000",
      },
    });

    expect(body.toolAllowlist).toEqual(["search", "fetch_page"]);
    expect(body.timeoutMs).toBe(45000);
  });

  it("builds mcp stdio create body with command args cwd env", () => {
    const body = itemToCreateBody("mcp", {
      id: "local-fs",
      name: "Local FS",
      description: "",
      enabled: true,
      settings: {
        transport: "stdio",
        serverUrl: "",
        command: "/usr/bin/npx",
        args: "-y @modelcontextprotocol/server-filesystem /data",
        cwd: "/home/agent",
        env: '{ "NODE_ENV": "production" }',
      },
    });

    expect(body.transport).toBe("stdio");
    expect(body.command).toBe("/usr/bin/npx");
    expect(body.args).toEqual([
      "-y",
      "@modelcontextprotocol/server-filesystem",
      "/data",
    ]);
    expect(body.cwd).toBe("/home/agent");
    expect(body.env).toEqual({ NODE_ENV: "production" });
  });

  it("maps stdio mcp dto fields into workspace item settings", () => {
    const item = mcpServerDtoToItem({
      id: "local-fs",
      name: "Local FS",
      transport: "stdio",
      command: "/usr/bin/npx",
      args: ["-y", "pkg"],
      cwd: "/tmp",
      env: { FOO: "bar" },
    });

    expect(item.settings?.command).toBe("/usr/bin/npx");
    expect(item.settings?.args).toBe("-y pkg");
    expect(item.settings?.cwd).toBe("/tmp");
    expect(item.settings?.env).toContain("FOO");
  });

  it("maps mcp tool manifest names for datalink detection", () => {
    const item = mcpServerDtoToItem({
      id: "datalink",
      name: "datalink",
      transport: "streamable-http",
      serverUrl: "https://datalink.example/mcp",
      apiUrl: "https://datalink.example",
      toolManifest: [
        { name: "datalink_show" },
        { name: "datalink_explore" },
        { name: "add_table" },
      ],
    });

    expect(item.settings?.toolCount).toBe("3");
    expect(item.settings?.toolNames).toContain("datalink_show");
    expect(item.settings?.toolNames).toContain("add_table");
    expect(item.settings?.apiUrl).toBe("https://datalink.example");
    expect(itemToCreateBody("mcp", item)).toMatchObject({
      apiUrl: "https://datalink.example",
    });
  });

  it("builds skill resource binding bodies", () => {
    const item = {
      id: "sales-skill",
      name: "Sales Skill",
      description: "",
      enabled: true,
      revision: 3,
      settings: {
        defaultDbIds: "sales-pg, finance-pg",
        defaultKbIds: "sales-docs",
        defaultMcpIds: "notion",
        modelProfileId: "qwen-plus",
      },
    };

    expect(itemToCreateBody("skill", item)).toMatchObject({
      defaultDbIds: ["sales-pg", "finance-pg"],
      defaultKbIds: ["sales-docs"],
      defaultMcpIds: ["notion"],
      modelProfileId: "qwen-plus",
    });
    expect(itemToPatchBody("skill", item)).toMatchObject({
      defaultDbIds: ["sales-pg", "finance-pg"],
      defaultKbIds: ["sales-docs"],
      defaultMcpIds: ["notion"],
      modelProfileId: "qwen-plus",
      revision: 3,
    });
  });

  it("loads server authoritative session conversation through the config client", async () => {
    process.env.NEXT_PUBLIC_CONFIG_API_URL = "http://config.test";
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      success: true,
      data: {
        sessionId: "thread-1",
        messages: [{ id: "run-1:user", runId: "run-1", role: "user", source: "client", contentText: "hi", position: 1, createdAt: "now" }],
        runEventRefs: [{ runId: "run-1", eventCount: 3 }],
        checkpoints: [{
          runId: "run-1",
          status: "canceled",
          messageStartPosition: 1,
          messageEndPosition: 2,
          firstEventSeq: 1,
          lastEventSeq: 3,
          startedAt: "now",
          finishedAt: "later",
          errorMessage: "user-requested",
        }],
        toolCalls: [{ runId: "run-1", toolCallId: "call-1", status: "completed", toolName: "inspect_schema" }],
      },
    }), { headers: { "Content-Type": "application/json" }, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const conversation = await configApi.getSessionConversation("thread-1", 25);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://config.test/api/v1/sessions/thread-1/conversation?limit=25",
      expect.objectContaining({ headers: expect.objectContaining({ Accept: "application/json" }) }),
    );
    expect(conversation.sessionId).toBe("thread-1");
    expect(conversation.messages[0]?.contentText).toBe("hi");
    expect(conversation.checkpoints?.[0]?.status).toBe("canceled");
    expect(conversation.checkpoints?.[0]?.messageEndPosition).toBe(2);
    expect(conversation.toolCalls[0]?.toolName).toBe("inspect_schema");
  });

  it("creates a server-side session branch through the config client", async () => {
    process.env.NEXT_PUBLIC_CONFIG_API_URL = "http://config.test";
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      success: true,
      data: {
        id: "branch-thread-2",
        sessionId: "thread-2",
        threadId: "thread-2",
        parentSessionId: "thread-1",
        rootSessionId: "thread-1",
        forkRunId: "run-1",
        forkMessageEndPosition: 2,
        createdAt: "now",
        session: {
          id: "thread-2",
          threadId: "thread-2",
          title: "Branch",
          titleSource: "fallback",
          createdAt: "now",
          updatedAt: "now",
          lastMessageAt: "now",
        },
      },
    }), { headers: { "Content-Type": "application/json" }, status: 201 }));
    vi.stubGlobal("fetch", fetchMock);

    const branch = await configApi.createSessionBranch("thread-1", { runId: "run-1" });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://config.test/api/v1/sessions/thread-1/branches",
      expect.objectContaining({
        body: JSON.stringify({ runId: "run-1" }),
        method: "POST",
      }),
    );
    expect(branch.session.id).toBe("thread-2");
    expect(branch.forkRunId).toBe("run-1");
  });

  it("loads and patches server sessions through the config client", async () => {
    process.env.NEXT_PUBLIC_CONFIG_API_URL = "http://config.test";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        success: true,
        data: {
          sessions: [
            {
              id: "thread-1",
              threadId: "thread-1",
              title: "渠道订单分析",
              titleSource: "llm",
              updatedAt: "2026-06-27T10:05:00.000Z",
            },
          ],
          nextCursor: "cursor-2",
        },
      }), { headers: { "Content-Type": "application/json" }, status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        success: true,
        data: {
          sessionId: "thread-1",
          title: "我的复盘",
          titleSource: "user",
          updatedAt: "2026-06-27T10:06:00.000Z",
        },
      }), { headers: { "Content-Type": "application/json" }, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({
      success: true,
      data: {
        sessionId: "thread-1",
        deleted: true,
        deletedSessionIds: ["thread-1", "thread-1-branch"],
      },
    }), { headers: { "Content-Type": "application/json" }, status: 200 }));

    const list = await configApi.listSessions({ limit: 20, cursor: "cursor-1" });
    const patched = await configApi.patchSessionTitle("thread-1", "我的复盘");
    const deleted = await configApi.deleteSession("thread-1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://config.test/api/v1/sessions?limit=20&cursor=cursor-1",
      expect.objectContaining({ headers: expect.objectContaining({ Accept: "application/json" }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://config.test/api/v1/sessions/thread-1",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ title: "我的复盘" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://config.test/api/v1/sessions/thread-1",
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(list.sessions[0]?.title).toBe("渠道订单分析");
    expect(list.nextCursor).toBe("cursor-2");
    expect(patched.sessionId).toBe("thread-1");
    expect(patched.titleSource).toBe("user");
    expect(deleted.deleted).toBe(true);
    expect(deleted.deletedSessionIds).toEqual(["thread-1", "thread-1-branch"]);
  });

  it("calls datalink endpoints through the config client", async () => {
    process.env.NEXT_PUBLIC_CONFIG_API_URL = "http://config.test";
    const fetchMock = vi.fn().mockImplementation(() => new Response(JSON.stringify({
      success: true,
      data: {
        servers: [{ id: "datalink", name: "datalink" }],
        graph: { nodes: [], edges: [] },
        server: { id: "datalink", name: "datalink" },
        result: "ok",
      },
    }), { headers: { "Content-Type": "application/json" }, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await configApi.listDatalinkServers();
    await configApi.getDatalinkGraph("datalink");
    await configApi.exploreDatalink("datalink", { query: "satscores", focus: "schema" });
    await configApi.addDatalinkTable("datalink", { source: "/data/orders.csv", sourceType: "csv" });
    await configApi.removeDatalinkTable("datalink", { tableId: "table:orders" });
    await configApi.rebuildDatalink("datalink");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://config.test/api/v1/datalink/servers",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://config.test/api/v1/datalink/datalink/graph",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://config.test/api/v1/datalink/datalink/explore",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ query: "satscores", focus: "schema" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://config.test/api/v1/datalink/datalink/tables",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ source: "/data/orders.csv", sourceType: "csv" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "http://config.test/api/v1/datalink/datalink/tables",
      expect.objectContaining({
        method: "DELETE",
        body: JSON.stringify({ tableId: "table:orders" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      "http://config.test/api/v1/datalink/datalink/rebuild",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("calls backend implemented data-task extension endpoints", async () => {
    process.env.NEXT_PUBLIC_CONFIG_API_URL = "http://config.test";
    const fetchMock = vi.fn().mockImplementation(
      () =>
        new Response(JSON.stringify({ success: true, data: {} }), {
          headers: { "Content-Type": "application/json" },
          status: 200,
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await configApi.cancelRun("run-1", "user-click");
    await configApi.exportArtifact("artifact-1", "xlsx", "same-export");
    await configApi.listWorkspaceFiles({ scope: "workspace", origin: ["uploaded", "saved"] });
    await configApi.getDatasourceSchema("db-1", { q: "orders", includeStats: true });
    await configApi.listQueryHistory({ sessionId: "thread-1", datasourceId: "db-1", favorite: true });
    await configApi.favoriteQueryHistory("query-1", true);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://config.test/api/v1/runs/run-1/cancel",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ reason: "user-click" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://config.test/api/v1/artifacts/artifact-1/export",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ format: "xlsx", idempotencyKey: "same-export" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://config.test/api/v1/files?scope=workspace&origin=uploaded%2Csaved",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://config.test/api/v1/datasources/db-1/schema?q=orders&includeStats=true",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "http://config.test/api/v1/query-history?sessionId=thread-1&datasourceId=db-1&favorite=true",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      "http://config.test/api/v1/query-history/query-1/favorite",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("builds datasource table preview requests", async () => {
    process.env.NEXT_PUBLIC_CONFIG_API_URL = "http://config.test";
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      success: true,
      data: {
        columns: [{ name: "order_id", type: "VARCHAR" }],
        rows: [{ order_id: "A001" }],
        total: 42,
        hasMore: true,
      },
    }), { headers: { "Content-Type": "application/json" }, status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const preview = await configApi.getDatasourceTablePreview("sales-pg", "orders", {
      schema: "public",
      limit: 25,
      offset: 50,
      orderBy: "created_at",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "http://config.test/api/v1/datasources/sales-pg/tables/orders/preview?schema=public&limit=25&offset=50&orderBy=created_at",
      expect.objectContaining({ headers: expect.objectContaining({ Accept: "application/json" }) }),
    );
    expect(preview.rows[0]?.order_id).toBe("A001");
    expect(preview.hasMore).toBe(true);
  });
});
