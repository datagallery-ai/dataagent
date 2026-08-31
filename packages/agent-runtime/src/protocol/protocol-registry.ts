import { validateProtocolDefinition } from "./definition-validator.js";
import type { AgentProtocolDefinition } from "./types.js";

export type RegisteredProtocolDefinition = AgentProtocolDefinition<any>;

export class ProtocolRegistry {
  private readonly definitions = new Map<string, RegisteredProtocolDefinition>();

  register(definition: RegisteredProtocolDefinition): void {
    validateProtocolDefinition(definition);
    const key = protocolDefinitionKey(definition.id, definition.version);
    if (this.definitions.has(key)) {
      throw new Error(`PROTOCOL_ALREADY_REGISTERED:${key}`);
    }
    this.definitions.set(key, definition);
  }

  /** Replace a registered definition in place (same id@version). Used when routing
   * resolves first and the definition is later rebuilt with extracted requirements. */
  replace(definition: RegisteredProtocolDefinition): void {
    validateProtocolDefinition(definition);
    this.definitions.set(protocolDefinitionKey(definition.id, definition.version), definition);
  }

  list(): RegisteredProtocolDefinition[] {
    return [...this.definitions.values()];
  }

  find(id: string, version: string): RegisteredProtocolDefinition | undefined {
    return this.definitions.get(protocolDefinitionKey(id, version));
  }
}

export const protocolDefinitionKey = (id: string, version: string): string => `${id}@${version}`;
