import fs from 'fs';
import path from 'path';
import { createLogger, logPath } from '../../../../scripts/lib/logging.mjs';

export enum LogLevel {
  DEBUG = 0,
  INFO = 1,
  WARN = 2,
  ERROR = 3,
}

interface LoggerConfig {
  logDir: string;
  logFileName: string;
  maxLogSize: number; // bytes
  maxLogFiles: number;
  debugMode: boolean;
}

class Logger {
  private config: LoggerConfig;
  private logFilePath: string;
  private currentLogLevel: LogLevel;
  private writer: ReturnType<typeof createLogger>;

  constructor(config?: Partial<LoggerConfig>) {
    this.config = {
      logDir: config?.logDir || path.dirname(logPath('tui')),
      logFileName: 'tui.log',
      maxLogSize: config?.maxLogSize || 5_000_000,
      maxLogFiles: config?.maxLogFiles || 3,
      debugMode: config?.debugMode || false,
    };
    this.logFilePath = path.join(this.config.logDir, this.config.logFileName);
    this.writer = createLogger({ file: 'tui', directory: this.config.logDir,
      maxBytes: this.config.maxLogSize, backups: this.config.maxLogFiles });
    this.currentLogLevel = this.config.debugMode ? LogLevel.DEBUG : LogLevel.INFO;
  }

  /**
   * Enable or disable debug mode
   */
  setDebugMode(enabled: boolean): void {
    this.config.debugMode = enabled;
    this.currentLogLevel = enabled ? LogLevel.DEBUG : LogLevel.INFO;
  }

  /**
   * Set log level
   */
  setLogLevel(level: LogLevel): void {
    this.currentLogLevel = level;
  }

  /**
   * Log debug message
   */
  debug(message: string, metadata?: any): void {
    if (this.currentLogLevel <= LogLevel.DEBUG) {
      this.writer.debug(message, metadata);
    }
  }

  /**
   * Log info message
   */
  info(message: string, metadata?: any): void {
    if (this.currentLogLevel <= LogLevel.INFO) {
      this.writer.info(message, metadata);
    }
  }

  /**
   * Log warning message
   */
  warn(message: string, metadata?: any): void {
    if (this.currentLogLevel <= LogLevel.WARN) {
      this.writer.warn(message, metadata);
    }
  }

  /**
   * Log error message
   */
  error(message: string, error?: Error | any, metadata?: any): void {
    if (this.currentLogLevel <= LogLevel.ERROR) {
      this.writer.error(message, error, metadata);
    }
  }

  /**
   * Log protocol event
   */
  protocolEvent(eventType: string, data: any): void {
    this.debug(`Protocol event: ${eventType}`, { eventType, data });
  }

  /**
   * Log command execution
   */
  commandExecution(command: string, args?: any): void {
    this.info(`Command executed: ${command}`, { command, args });
  }

  /**
   * Log API call
   */
  apiCall(method: string, endpoint: string, status?: number, duration?: number): void {
    const metadata = {
      method,
      endpoint,
      status,
      duration: duration ? `${duration}ms` : undefined,
    };
    this.info(`API call: ${method} ${endpoint}`, metadata);
  }

  /**
   * Get log file path
   */
  getLogFilePath(): string {
    return this.logFilePath;
  }

  /**
   * Clear all log files
   */
  clearLogs(): void {
    try {
      // Remove current log file
      if (fs.existsSync(this.logFilePath)) {
        fs.unlinkSync(this.logFilePath);
      }

      // Remove rotated log files
      for (let i = 1; i <= this.config.maxLogFiles; i++) {
        const rotatedPath = path.join(
          this.config.logDir,
          `${this.config.logFileName}.${i}`
        );
        if (fs.existsSync(rotatedPath)) {
          fs.unlinkSync(rotatedPath);
        }
      }

      this.info('Log files cleared');
    } catch (error) {
      this.error('Failed to clear log files', error);
    }
  }
}

// Create singleton instance
let loggerInstance: Logger | null = null;

/**
 * Get logger instance
 */
export function getLogger(): Logger {
  if (!loggerInstance) {
    loggerInstance = new Logger();
  }
  return loggerInstance;
}

/**
 * Initialize logger with configuration
 */
export function initLogger(config?: Partial<LoggerConfig>): Logger {
  loggerInstance = new Logger(config);
  return loggerInstance;
}

/**
 * Enable debug mode
 */
export function enableDebugMode(): void {
  getLogger().setDebugMode(true);
}

/**
 * Disable debug mode
 */
export function disableDebugMode(): void {
  getLogger().setDebugMode(false);
}

// Export default logger instance
export default getLogger;
