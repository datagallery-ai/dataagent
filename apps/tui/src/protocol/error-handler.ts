/**
 * Error classification and handling utilities
 */

import { getLogger } from '../utils/logger.js';

export enum ErrorCategory {
  NETWORK = 'network',
  CONFIG = 'config',
  API = 'api',
  VALIDATION = 'validation',
  STREAM = 'stream',
  UNKNOWN = 'unknown',
}

export interface ClassifiedError {
  category: ErrorCategory;
  message: string;
  userMessage: string;
  code?: string;
  statusCode?: number;
  retryable: boolean;
  suggestedAction?: string;
}

export interface ErrorLogEntry {
  timestamp: Date;
  error: ClassifiedError;
  context?: Record<string, unknown> | undefined;
}

class ErrorLogger {
  private logs: ErrorLogEntry[] = [];
  private maxLogs = 100;

  log(error: ClassifiedError, context?: Record<string, unknown>): void {
    this.logs.push({
      timestamp: new Date(),
      error,
      context,
    });

    // Keep only the most recent logs
    if (this.logs.length > this.maxLogs) {
      this.logs.shift();
    }

    getLogger().error('TUI runtime error', undefined, {
      category: error.category,
      message: error.message,
      code: error.code,
      statusCode: error.statusCode,
      context,
    });
  }

  getLogs(): ErrorLogEntry[] {
    return [...this.logs];
  }

  getRecentLogs(count: number): ErrorLogEntry[] {
    return this.logs.slice(-count);
  }

  clear(): void {
    this.logs = [];
  }
}

export const errorLogger = new ErrorLogger();

/**
 * Classify an error and generate user-friendly messages
 */
export function classifyError(error: unknown): ClassifiedError {
  // Handle CopilotKitClientError
  if (error && typeof error === 'object' && 'code' in error && 'statusCode' in error) {
    const code = String((error as { code?: unknown }).code ?? '');
    const statusCode = Number((error as { statusCode?: unknown }).statusCode ?? 0);
    const message = error instanceof Error ? error.message : String(error);

    switch (code) {
      case 'NETWORK_ERROR':
        return {
          category: ErrorCategory.NETWORK,
          message,
          userMessage: 'Unable to connect to the agent service',
          code,
          retryable: true,
          suggestedAction: 'Check your network connection and verify the service is running',
        };

      case 'PROVIDER_CONFIG_MISSING':
        return {
          category: ErrorCategory.CONFIG,
          message,
          userMessage: 'LLM provider is not configured',
          code,
          statusCode,
          retryable: false,
          suggestedAction: 'Set LLM_API_KEY and other required environment variables',
        };

      case 'VALIDATION_ERROR':
        return {
          category: ErrorCategory.VALIDATION,
          message,
          userMessage: 'Invalid request data',
          code,
          statusCode,
          retryable: false,
          suggestedAction: 'Check your input and try again',
        };

      case 'INVALID_CONTENT_TYPE':
      case 'EMPTY_STREAM':
        return {
          category: ErrorCategory.STREAM,
          message,
          userMessage: 'Failed to receive agent response',
          code,
          statusCode,
          retryable: true,
          suggestedAction: 'The connection may be interrupted. Retrying...',
        };

      case 'HTTP_ERROR':
        if (statusCode >= 500) {
          return {
            category: ErrorCategory.API,
            message,
            userMessage: 'Agent service is temporarily unavailable',
            code,
            statusCode,
            retryable: true,
            suggestedAction: 'The service may be experiencing issues. Retrying...',
          };
        } else if (statusCode === 401 || statusCode === 403) {
          return {
            category: ErrorCategory.CONFIG,
            message,
            userMessage: 'Authentication failed',
            code,
            statusCode,
            retryable: false,
            suggestedAction: 'Check your API credentials and permissions',
          };
        } else if (statusCode === 404) {
          return {
            category: ErrorCategory.CONFIG,
            message,
            userMessage: 'Agent endpoint not found',
            code,
            statusCode,
            retryable: false,
            suggestedAction: 'Verify the agent ID and runtime URL are correct',
          };
        }
        return {
          category: ErrorCategory.API,
          message,
          userMessage: `API error: ${message}`,
          code,
          statusCode,
          retryable: false,
        };
    }
  }

  // Handle standard Error objects
  if (error instanceof Error) {
    const message = error.message.toLowerCase();

    // Network-related errors
    if (
      message.includes('fetch') ||
      message.includes('network') ||
      message.includes('connection') ||
      message.includes('econnrefused') ||
      message.includes('timeout')
    ) {
      return {
        category: ErrorCategory.NETWORK,
        message: error.message,
        userMessage: 'Network connection failed',
        retryable: true,
        suggestedAction: 'Check your network and service availability',
      };
    }

    // Configuration errors
    if (
      message.includes('config') ||
      message.includes('not found') ||
      message.includes('invalid url')
    ) {
      return {
        category: ErrorCategory.CONFIG,
        message: error.message,
        userMessage: 'Configuration error',
        retryable: false,
        suggestedAction: 'Review your configuration settings',
      };
    }

    return {
      category: ErrorCategory.UNKNOWN,
      message: error.message,
      userMessage: error.message,
      retryable: false,
    };
  }

  // Handle unknown error types
  const errorString = String(error);
  return {
    category: ErrorCategory.UNKNOWN,
    message: errorString,
    userMessage: 'An unexpected error occurred',
    retryable: false,
  };
}

/**
 * Format error for display to user
 */
export function formatErrorMessage(error: ClassifiedError): string {
  let message = error.userMessage;

  if (error.suggestedAction) {
    message += `\n${error.suggestedAction}`;
  }

  return message;
}

/**
 * Determine if an error should trigger a retry
 */
export function shouldRetry(error: ClassifiedError, attemptCount: number, maxAttempts: number): boolean {
  if (attemptCount >= maxAttempts) {
    return false;
  }

  return error.retryable;
}

/**
 * Calculate exponential backoff delay
 */
export function getRetryDelay(attemptCount: number, baseDelay: number = 1000, maxDelay: number = 10000): number {
  const delay = Math.min(baseDelay * Math.pow(2, attemptCount), maxDelay);
  // Add jitter to prevent thundering herd
  const jitter = Math.random() * 0.3 * delay;
  return delay + jitter;
}
