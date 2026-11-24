import { describe, expect, it } from "vitest";
import { firstValueFrom, toArray } from "rxjs";
import { transformChunks, verifyEvents } from "@ag-ui/client";
import { StreamHttpAgent } from "../stream-http-agent";

const started = { type: "RUN_STARTED", threadId: "thread-1", runId: "run-1" };
const finished = { type: "RUN_FINISHED", threadId: "thread-1", runId: "run-1" };

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

function frame(event: unknown): string {
  return `data: ${JSON.stringify(event)}\n\n`;
}

describe("StreamHttpAgent", () => {
  it("posts HITL answers and preserves the waiting terminal status", async () => {
    let body: unknown;
    const pending = [{ type: "human_feedback", interruptId: "ask", message: "Which ID?" }];
    const agent = new StreamHttpAgent({ fetch: async (_url, init) => {
      body = JSON.parse(String(init.body));
      return sseResponse([frame(started), frame({ type: "CUSTOM", name: "human_feedback", value: pending }),
        frame({ ...finished, metadata: { status: "waiting_for_input" } })]);
    } });
    const events = await firstValueFrom(agent.run({
      threadId: "thread-1", runId: "run-1", messages: [], tools: [], context: [], state: {},
      forwardedProps: { hitlAnswers: { first: "user_id" } },
    }).pipe(toArray()));
    expect(body).toEqual({ threadId: "thread-1", runId: "run-1", messages: [],
      answers: { first: "user_id" }, tools: [], context: [], state: {}, forwardedProps: {} });
    expect(events[1]).toMatchObject({ name: "human_feedback", value: pending });
    expect(events.at(-1)).toMatchObject({ metadata: { status: "waiting_for_input" } });
  });
  it("posts one user message and rewrites child progress for the old client", async () => {
    let captured: { url: string; body: unknown; accept: string | null } | undefined;
    const agent = new StreamHttpAgent({
      threadId: "thread-1",
      fetch: async (url, init) => {
        captured = {
          url,
          body: JSON.parse(String(init.body)),
          accept: new Headers(init.headers).get("accept"),
        };
        return sseResponse([
          frame(started).slice(0, 12),
          frame(started).slice(12),
          ": keep\n\n",
          frame({ type: "SUBAGENT_STARTED", subagentRunId: "child", name: "nl2sql" }),
          frame({ type: "STEP_STARTED", stepName: "model", subagentRunId: "child" }),
          frame({ type: "TEXT_MESSAGE_START", messageId: "m", role: "assistant" }),
          frame({ type: "TEXT_MESSAGE_CONTENT", messageId: "m", delta: "hi" }),
          frame({ type: "TEXT_MESSAGE_END", messageId: "m" }),
          frame({ type: "STEP_STARTED", stepName: "model" }),
          frame({ type: "STEP_FINISHED", stepName: "model" }),
          frame(finished),
        ]);
      },
    });

    const events = await firstValueFrom(
      agent
        .run({
          threadId: "thread-1",
          runId: "run-1",
          messages: [
            { id: "old", role: "assistant", content: "earlier" },
            {
              id: "user",
              role: "user",
              content: [
                { type: "text", text: "hello" },
                { type: "image", source: { type: "url", value: "x" } },
              ],
            },
          ],
          tools: [{ name: "frontend_echo", description: "echo", parameters: {} }],
          context: [{ description: "datasource_id", value: "orders" }],
          state: { run_config: { enabledSkillIds: ["skill-1"] } },
          forwardedProps: { datasourceId: "orders-db" },
        })
        .pipe(toArray()),
    );

    expect(captured).toEqual({
      url: "/api/v1/runs",
      accept: "text/event-stream",
      body: {
        threadId: "thread-1",
        runId: "run-1",
        messages: [{ id: "user", role: "user", content: "hello" }],
        tools: [],
        context: [],
        state: {},
        forwardedProps: {},
      },
    });
    expect(events.map((event) => event.type)).toEqual([
      "RUN_STARTED",
      "CUSTOM",
      "STEP_STARTED",
      "TEXT_MESSAGE_START",
      "TEXT_MESSAGE_CONTENT",
      "TEXT_MESSAGE_END",
      "STEP_STARTED",
      "STEP_FINISHED",
      "STEP_FINISHED",
      "RUN_FINISHED",
    ]);
    expect(events[1]).toMatchObject({
      name: "subagent",
      value: { type: "SUBAGENT_STARTED", subagentRunId: "child", name: "nl2sql" },
    });
    expect(events[2]).toMatchObject({
      stepName: "child/model",
      rawStepName: "model",
      subagentRunId: "child",
    });
    expect(events[7]).toMatchObject({ stepName: "model" });
  });

  it("lets the AG-UI verifier accept a child step with the same name as the parent", async () => {
    const agent = new StreamHttpAgent({
      fetch: async () =>
        sseResponse([
          frame(started),
          frame({ type: "STEP_STARTED", stepName: "model" }),
          frame({ type: "SUBAGENT_STARTED", subagentRunId: "child", name: "nl2sql", parentToolCallId: "task" }),
          frame({ type: "STEP_STARTED", stepName: "model", subagentRunId: "child" }),
          frame({ type: "TEXT_MESSAGE_CONTENT", subagentRunId: "child", messageId: "private", delta: "hidden" }),
          frame({ type: "SUBAGENT_ERROR", subagentRunId: "child", message: "bad" }),
          frame({ type: "STEP_FINISHED", stepName: "model" }),
          frame(finished),
        ]),
    });
    const events = await firstValueFrom(
      agent
        .run({
          threadId: "thread-1",
          runId: "run-1",
          messages: [{ id: "user", role: "user", content: "hello" }],
          tools: [],
          context: [],
          state: {},
          forwardedProps: {},
        })
        .pipe(transformChunks(undefined), verifyEvents(undefined), toArray()),
    );
    expect(events.map((event) => event.type)).toEqual([
      "RUN_STARTED",
      "STEP_STARTED",
      "CUSTOM",
      "STEP_STARTED",
      "CUSTOM",
      "STEP_FINISHED",
      "STEP_FINISHED",
      "RUN_FINISHED",
    ]);
    expect(events.some((event) => "delta" in event && event.delta === "hidden")).toBe(false);
  });

  it("reports a blank user message through the observable", async () => {
    const agent = new StreamHttpAgent({
      fetch: async () => {
        throw new Error("should not fetch");
      },
    });
    const input = {
      threadId: "thread-1",
      runId: "run-1",
      messages: [{ id: "user", role: "user" as const, content: "   " }],
      tools: [],
      context: [],
      state: {},
      forwardedProps: {},
    };
    expect(() => agent.run(input)).not.toThrow();
    await expect(firstValueFrom(agent.run(input))).rejects.toThrow(/nonempty text/);
    const missingUser = {
      ...input,
      messages: [{ id: "a", role: "assistant" as const, content: "only" }],
    };
    await expect(firstValueFrom(agent.run(missingUser))).rejects.toThrow(/user message/);
  });

  it("surfaces a busy session as HTTP 409", async () => {
    const agent = new StreamHttpAgent({
      fetch: async () =>
        new Response(JSON.stringify({
          success: false,
          error: { code: "CONFLICT", message: "A run is already active for this session" },
        }), {
          status: 409,
          headers: { "content-type": "application/json" },
        }),
    });
    await expect(
      firstValueFrom(
        agent.run({
          threadId: "thread-1",
          runId: "run-1",
          messages: [{ id: "user", role: "user", content: "hello" }],
          tools: [],
          context: [],
          state: {},
          forwardedProps: {},
        }),
      ),
    ).rejects.toThrow("HTTP 409: A run is already active for this session");
  });

  it("rejects a stream that ends without a terminal event", async () => {
    const agent = new StreamHttpAgent({
      fetch: async () => sseResponse([frame(started)]),
    });
    await expect(
      firstValueFrom(
        agent
          .run({
            threadId: "thread-1",
            runId: "run-1",
            messages: [{ id: "user", role: "user", content: "hello" }],
            tools: [],
            context: [],
            state: {},
            forwardedProps: {},
          })
          .pipe(toArray()),
      ),
    ).rejects.toThrow(/INCOMPLETE_STREAM/);
  });

  it("ends an aborted run as cancelled instead of a failed stream", async () => {
    const agent = new StreamHttpAgent({
      fetch: (_url, init) =>
        new Promise((_resolve, reject) => {
          const abort = () => reject(new DOMException("The operation was aborted", "AbortError"));
          if (init.signal?.aborted) {
            abort();
            return;
          }
          init.signal?.addEventListener("abort", abort);
        }),
    });
    const pending = firstValueFrom(
      agent
        .run({
          threadId: "thread-1",
          runId: "run-1",
          messages: [{ id: "user", role: "user", content: "hello" }],
          tools: [],
          context: [],
          state: {},
          forwardedProps: {},
        })
        .pipe(toArray()),
    );
    agent.abortRun();
    const events = await pending;
    expect(events).toEqual([{ type: "RUN_ERROR", message: "The run was cancelled", code: "abort" }]);
  });
});
