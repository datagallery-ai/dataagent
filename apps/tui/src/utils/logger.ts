import fs from 'fs';
import path from 'path';
import os from 'os';

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

  constructor(config?: Partial<LoggerConfig>) {
    const defaultLogDir = path.join(os.homedir(), '.datafoundry');

    this.config = {
      logDir: config?.logDir || defaultLogDir,
      logFileName: config?.logFileName || 'tui.log',
      maxLogSize: config?.maxLogSize || 10 * 1024 * 1024, // 10MB
      maxLogFiles: config?.maxLogFiles || 5,
      debugMode: config?.debugMode || false,
    };

    this.logFilePath = path.join(this.config.logDir, this.config.logFileName);
    this.currentLogLevel = this.config.debugMode ? LogLevel.DEBUG : LogLevel.INFO;

    this.ensureLogDirectory();
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
   * Ensure log directory exists
   */
  private ensureLogDirectory(): void {
    try {
      if (!fs.existsSync(this.config.logDir)) {
        fs.mkdirSync(this.config.logDir, { recursive: true });
      }
    } catch (error) {
      // Silently fail - logging should not crash the app
      void error;
    }
  }

  /**
   * Format log message with timestamp and level
   */
  private formatMessage(level: string, message: string, metadata?: any): string {
    const timestamp = new Date().toISOString();
    const metaStr = metadata ? ` ${JSON.stringify(metadata)}` : '';
    return `[${timestamp}] [${level}] ${message}${metaStr}\n`;
  }

  /**
   * Write log message to file
   */
  private writeToFile(message: string): void {
    try {
      // Check if log rotation is needed
      this.rotateLogsIfNeeded();

      // Append to log file
      fs.appendFileSync(this.logFilePath, message, 'utf8');
    } catch (error) {
      // Silently fail - logging should not crash the app
      void error;
    }
  }

  /**
   * Rotate logs if file size exceeds maximum
   */
  private rotateLogsIfNeeded(): void {
    try {
      if (!fs.existsSync(this.logFilePath)) {
        return;
      }

      const stats = fs.statSync(this.logFilePath);

      if (stats.size >= this.config.maxLogSize) {
        // Rotate existing log files
        for (let i = this.config.maxLogFiles - 1; i > 0; i--) {
          const oldPath = path.join(
            this.config.logDir,
            `${this.config.logFileName}.${i}`
          );
          const newPath = path.join(
            this.config.logDir,
            `${this.config.logFileName}.${i + 1}`
          );

          if (fs.existsSync(oldPath)) {
            if (i === this.config.maxLogFiles - 1) {
              // Delete oldest log
              fs.unlinkSync(oldPath);
            } else {
              // Rename log file
              fs.renameSync(oldPath, newPath);
            }
          }
        }

        // Rotate current log file
        const rotatedPath = path.join(
          this.config.logDir,
          `${this.config.logFileName}.1`
        );
        fs.renameSync(this.logFilePath, rotatedPath);
      }
    } catch (error) {
      // Silently fail - rotation errors should not crash the app
      void error;
    }
  }

  /**
   * Log debug message
   */
  debug(message: string, metadata?: any): void {
    if (this.currentLogLevel <= LogLevel.DEBUG) {
      const formattedMessage = this.formatMessage('DEBUG', message, metadata);
      this.writeToFile(formattedMessage);
    }
  }

  /**
   * Log info message
   */
  info(message: string, metadata?: any): void {
    if (this.currentLogLevel <= LogLevel.INFO) {
      const formattedMessage = this.formatMessage('INFO', message, metadata);
      this.writeToFile(formattedMessage);
    }
  }

  /**
   * Log warning message
   */
  warn(message: string, metadata?: any): void {
    if (this.currentLogLevel <= LogLevel.WARN) {
      const formattedMessage = this.formatMessage('WARN', message, metadata);
      this.writeToFile(formattedMessage);
    }
  }

  /**
   * Log error message
   */
  error(message: string, error?: Error | any, metadata?: any): void {
    if (this.currentLogLevel <= LogLevel.ERROR) {
      const errorDetails = error ? {
        message: error.message,
        stack: error.stack,
        ...metadata,
      } : metadata;

      const formattedMessage = this.formatMessage('ERROR', message, errorDetails);
      this.writeToFile(formattedMessage);
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
