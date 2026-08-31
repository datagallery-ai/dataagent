import type { ContextPackage } from "../inventory/context-package.js";
import type { ToolObservationPackager } from "./tool-observation-packager.js";
import { toolObservationModelFromPackage } from "./tool-observation-projection-items.js";
import type { ToolObservationRunScope } from "./tool-observation-run-scope.js";

export class ToolObservationDispatcher {
  private readonly governedObjects = new WeakSet<object>();

  constructor(
    private readonly packager: ToolObservationPackager,
    private readonly runScope: ToolObservationRunScope
  ) {}

  /** Govern one raw tool observation through its exact adapter. */
  dispatch(toolName: string, rawResult: unknown): ContextPackage {
    const contextPackage = this.packager.packageToolObservation({
      toolName,
      rawResult,
      runScope: this.runScope
    });
    this.markGoverned(toolObservationModelFromPackage(contextPackage));
    return contextPackage;
  }

  /** Fail during tool registration when an exact adapter is missing. */
  assertAdapterRegistered(toolName: string): void {
    if (!this.packager.hasToolAdapter(toolName)) {
      throw new Error(`CONTEXT_ADAPTER_REQUIRED:${toolName}`);
    }
  }

  /** Return whether a value was emitted by this dispatcher's governed path. */
  isGoverned(value: unknown): boolean {
    return isObject(value) && this.governedObjects.has(value);
  }

  private markGoverned(value: unknown): void {
    if (isObject(value)) {
      this.governedObjects.add(value);
    }
  }
}

const isObject = (value: unknown): value is object =>
  (typeof value === "object" && value !== null) || typeof value === "function";
