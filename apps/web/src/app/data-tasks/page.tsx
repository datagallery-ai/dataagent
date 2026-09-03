"use client";

import { Component, type ReactNode } from "react";
import nextDynamic from "next/dynamic";

export const dynamic = "force-dynamic";

class WorkbenchErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <pre className="whitespace-pre-wrap p-6 text-sm text-red-700">
          {this.state.error.stack || this.state.error.message}
        </pre>
      );
    }
    return this.props.children;
  }
}

/**
 * Production entry: keep the route module tiny so the browser can paint a shell
 * before downloading the CopilotKit-heavy workbench chunk.
 */
const DataTasksApp = nextDynamic(() => import("./data-tasks-app"), {
  ssr: false,
  loading: () => (
    <div className="flex min-h-screen items-center justify-center bg-slate-50 text-sm text-slate-500">
      Loading workbench…
    </div>
  ),
});

export default function DataTasksPage() {
  return (
    <WorkbenchErrorBoundary>
      <DataTasksApp />
    </WorkbenchErrorBoundary>
  );
}
