import type {
  CopilotKitError,
  CopilotKitEvent,
  RunAgentInput,
} from "./types.js";
import { getLogger } from "../utils/logger.js";
import {
  classifyError,
  errorLogger,
  shouldRetry,
  getRetryDelay,
  type ClassifiedError,
} from "./error-handler.js";

export interface CopilotKitClientConfig {
  runtimeUrl: string;
  agent: string;
  fetchImpl?: typeof fetch | undefined;
  maxRetries?: number | undefined;
  retryBaseDelay?: number | undefined;
  connectionCheckInterval?: number | undefined;
  onConnectionStatusChange?: ((status: 'connected' | 'disconnected' | 'reconnecting' | 'error') => void) | undefined;
  onError?: ((error: ClassifiedError) => void) | undefined;
}

type SingleRouteEnvelope = {
  method: "agent/run";
  params: {
    agentId: string;
  };
  body: {
    threadId: string;
    runId: string;
    parentRunId?: string;
    state: unknown;
    messages: RunAgentInput["messages"];
    tools: NonNullable<RunAgentInput["tools"]>;
    context: NonNullable<RunAgentInput["context"]>;
    forwardedProps: Record<string, unknown>;
    resume?: NonNullable<RunAgentInput["resume"]>;
  };
};

export class CopilotKitClient {
  private runtimeUrl: string;
  private agent: string;
  private fetchImpl: typeof fetch;
  private maxRetries: number;
  private retryBaseDelay: number;
  private connectionCheckInterval: number;
  private onConnectionStatusChange?: ((status: 'connected' | 'disconnected' | 'reconnecting' | 'error') => void) | undefined;
  private onError?: ((error: ClassifiedError) => void) | undefined;
  private reconnectTimer?: NodeJS.Timeout | undefined;
  private isReconnecting: boolean = false;

  constructor(config: CopilotKitClientConfig) {
    this.runtimeUrl = config.runtimeUrl;
    this.agent = config.agent;
    this.fetchImpl = config.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.maxRetries = config.maxRetries ?? 3;
    this.retryBaseDelay = config.retryBaseDelay ?? 1000;
    this.connectionCheckInterval = config.connectionCheckInterval ?? 30000;
    this.onConnectionStatusChange = config.onConnectionStatusChange;
    this.onError = config.onError;
  }

  /**
   * Start monitoring connection health
   */
  startConnectionMonitoring(): void {
    this.stopConnectionMonitoring();
    void this.checkConnection();

    this.reconnectTimer = setInterval(() => {
      this.checkConnection();
    }, this.connectionCheckInterval);
  }

  /**
   * Stop monitoring connection health
   */
  stopConnectionMonitoring(): void {
    if (this.reconnectTimer) {
      clearInterval(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
  }

  /**
   * Check if the service is reachable
   */
  private async checkConnection(): Promise<boolean> {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000);

      const response = await this.fetchImpl(this.runtimeUrl.replace(/\/api\/.*$/, '/healthz'), {
        method: 'GET',
        signal: controller.signal,
      }).finally(() => clearTimeout(timeoutId));

      const isConnected = response.ok;

      if (isConnected) {
        this.isReconnecting = false;
        this.onConnectionStatusChange?.('connected');
      }

      return isConnected;
    } catch (error) {
      if (!this.isReconnecting) {
        this.isReconnecting = true;
        this.onConnectionStatusChange?.('disconnected');

        // Attempt to reconnect
        this.attemptReconnect();
      }
      return false;
    }
  }

  /**
   * Attempt to reconnect to the service
   */
  private async attemptReconnect(): Promise<void> {
    let attempt = 0;

    while (this.isReconnecting && attempt < this.maxRetries) {
      this.onConnectionStatusChange?.('reconnecting');

      const delay = getRetryDelay(attempt, this.retryBaseDelay);
      await new Promise(resolve => setTimeout(resolve, delay));

      const connected = await this.checkConnection();
      if (connected) {
        this.isReconnecting = false;
        this.onConnectionStatusChange?.('connected');
        return;
      }

      attempt++;
    }

    if (this.isReconnecting) {
      this.isReconnecting = false;
      this.onConnectionStatusChange?.('error');

      const error = classifyError(new Error('Failed to reconnect after multiple attempts'));
      errorLogger.log(error, { maxRetries: this.maxRetries });
      this.onError?.(error);
    }
  }

  /**
   * Cleanup resources
   */
  dispose(): void {
    this.stopConnectionMonitoring();
  }

  async *runAgent(input: RunAgentInput): AsyncGenerator<CopilotKitEvent> {
    let attempt = 0;
    let lastError: ClassifiedError | undefined;

    while (attempt <= this.maxRetries) {
      try {
        const response = await this.postRunAgent(input);

        if (!response.ok) {
          throw await this.errorFromResponse(response);
        }

        const contentType = response.headers.get("content-type");
        if (!contentType?.includes("text/event-stream")) {
          throw new CopilotKitClientError(
            `Expected text/event-stream, got ${contentType ?? "unknown"}`,
            "INVALID_CONTENT_TYPE",
            response.status,
          );
        }

        if (!response.body) {
          throw new CopilotKitClientError("Response body is empty", "EMPTY_STREAM");
        }

        // Success - reset reconnecting state if applicable
        if (this.isReconnecting) {
          this.isReconnecting = false;
          this.onConnectionStatusChange?.('connected');
        }

        yield* this.parseSSEStream(response.body);
        return; // Success
      } catch (error) {
        // Classify the error
        const classifiedError = classifyError(error);
        lastError = classifiedError;

        // Log the error
        errorLogger.log(classifiedError, {
          attempt,
          threadId: input.threadId,
          runId: input.runId,
        });

        // Notify error handler
        this.onError?.(classifiedError);

        // Determine if we should retry
        if (!shouldRetry(classifiedError, attempt, this.maxRetries)) {
          throw error; // Non-retryable or max attempts reached
        }

        // Calculate delay and retry
        attempt++;
        if (attempt <= this.maxRetries) {
          this.onConnectionStatusChange?.('reconnecting');
          const delay = getRetryDelay(attempt - 1, this.retryBaseDelay);
          await new Promise(resolve => setTimeout(resolve, delay));
        }
      }
    }

    // If we've exhausted all retries, throw the last error
    throw lastError ? new Error(lastError.userMessage) : new Error('Request failed after maximum retries');
  }

  private async postRunAgent(input: RunAgentInput): Promise<Response> {
    let response: Response;
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 60000); // 60s timeout

      response = await this.fetchImpl(this.runtimeUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
        },
        body: JSON.stringify(this.createEnvelope(input)),
        signal: controller.signal,
      }).finally(() => clearTimeout(timeoutId));
    } catch (error) {
      // Enhance error message for common network issues
      if (error instanceof Error) {
        if (error.name === 'AbortError') {
          throw new CopilotKitClientError(
            'Request timed out after 60 seconds',
            'NETWORK_ERROR',
          );
        }
        throw new CopilotKitClientError(
          `Network request failed: ${error.message}`,
          'NETWORK_ERROR',
        );
      }
      throw new CopilotKitClientError(
        `Network request failed: ${String(error)}`,
        'NETWORK_ERROR',
      );
    }
    return response;
  }

  private createEnvelope(input: RunAgentInput): SingleRouteEnvelope {
    const body: SingleRouteEnvelope["body"] = {
      threadId: input.threadId,
      runId: input.runId,
      state: input.state ?? {},
      messages: input.messages,
      tools: input.tools ?? [],
      context: input.context ?? [],
      forwardedProps: input.forwardedProps ?? {},
    };

    if (input.parentRunId !== undefined) {
      body.parentRunId = input.parentRunId;
    }
    if (input.resume !== undefined) {
      body.resume = input.resume;
    }

    return {
      method: "agent/run",
      params: {
        agentId: this.agent,
      },
      body,
    };
  }

  private async errorFromResponse(response: Response): Promise<CopilotKitClientError> {
    let message = `Unexpected status code: ${response.status}`;
    try {
      const text = await response.text();
      if (text) {
        message = text;
        const parsed = JSON.parse(text) as {
          error?: string | { message?: string };
          message?: string;
          details?: string;
        };
        const nestedMessage =
          typeof parsed.error === "object" ? parsed.error.message : undefined;
        message = nestedMessage ?? parsed.message ?? parsed.details ?? text;
      }
    } catch {
      // Keep the raw response text or generic status message when the body is not JSON.
    }

    if (response.status === 503) {
      return new CopilotKitClientError(
        "Provider configuration missing. Please check LLM_API_KEY and other provider settings.",
        "PROVIDER_CONFIG_MISSING",
        response.status,
      );
    }

    if (response.status === 400) {
      return new CopilotKitClientError(message, "VALIDATION_ERROR", response.status);
    }

    return new CopilotKitClientError(message, "HTTP_ERROR", response.status);
  }

  private async *parseSSEStream(
    stream: ReadableStream<Uint8Array>,
  ): AsyncGenerator<CopilotKitEvent> {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        yield* this.drainSSEBuffer(buffer, (remaining) => {
          buffer = remaining;
        });
      }

      buffer += decoder.decode();
      yield* this.drainSSEBuffer(`${buffer}\n\n`, (remaining) => {
        buffer = remaining;
      });
    } catch (error) {
      // Log stream parsing errors
      const classifiedError = classifyError(error);
      errorLogger.log(classifiedError, { context: 'parseSSEStream' });

      throw new CopilotKitClientError(
        `Stream parsing failed: ${error instanceof Error ? error.message : String(error)}`,
        'STREAM_ERROR',
      );
    } finally {
      reader.releaseLock();
    }
  }

  private *drainSSEBuffer(
    input: string,
    setRemaining: (value: string) => void,
  ): Generator<CopilotKitEvent> {
    let buffer = input;

    while (true) {
      const match = /\r?\n\r?\n/.exec(buffer);
      if (!match) break;

      const eventText = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);

      const event = this.parseSSEEvent(eventText);
      if (event) {
        yield event;
      }
    }

    setRemaining(buffer);
  }

  private parseSSEEvent(eventText: string): CopilotKitEvent | null {
    const dataLines = eventText
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart());

    if (dataLines.length === 0) return null;

    const data = dataLines.join("\n").trim();
    if (!data || data === "[DONE]") return null;

    try {
      return JSON.parse(data) as CopilotKitEvent;
    } catch (error) {
      if (error instanceof CopilotKitClientError) {
        throw error;
      }
      getLogger().warn("Failed to parse SSE event", {
        data: data.length > 2000 ? `${data.slice(0, 2000)}...` : data,
      });
      return null;
    }
  }
}

export class CopilotKitClientError extends Error implements CopilotKitError {
  public code?: string | undefined;
  public statusCode?: number | undefined;

  constructor(message: string, code?: string, statusCode?: number) {
    super(message);
    this.name = "CopilotKitClientError";
    this.code = code;
    this.statusCode = statusCode;
  }
}
