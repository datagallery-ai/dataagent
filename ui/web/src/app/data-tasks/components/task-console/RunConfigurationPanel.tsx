import { useState, type ReactNode } from "react";
import type { LiveRun } from "../../live-run-state";
import { panelShellClass, panelTitleClass, sectionLabelClass } from "../../ui-tokens";

export function hasRunConfigurationSignals(liveRun: LiveRun): boolean {
  return Boolean(
    liveRun.resolvedRunConfig ||
      liveRun.skillSelection ||
      liveRun.goal ||
      liveRun.contextReports.length > 0,
  );
}

export function RunConfigurationPanel({ liveRun }: { liveRun: LiveRun }) {
  const [expanded, setExpanded] = useState(false);

  if (!hasRunConfigurationSignals(liveRun)) return null;

  const config = liveRun.resolvedRunConfig;
  const skillSelection = liveRun.skillSelection;
  const goal = liveRun.goal;
  const contextReports = liveRun.contextReports.slice(0, 3);

  return (
    <section className={panelShellClass}>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-lg text-left transition-colors duration-200 hover:bg-surface-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-light/50"
      >
        <h3 className={panelTitleClass}>Run Diagnostics</h3>
        <span
          aria-hidden="true"
          className={[
            "inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-light transition-transform duration-200",
            expanded ? "rotate-180" : "",
          ].join(" ")}
        >
          <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
            <path d="m4 6 4 4 4-4" />
          </svg>
        </span>
      </button>
      {expanded ? (
      <div className="mt-3 grid gap-2">
        <DiagnosticCard title="Resolved Resources">
          {config ? (
            <div className="grid gap-1.5">
              <ConfigLine label="Datasource" value={config.activeDatasourceId} />
              <ConfigLine label="Model" value={config.activeLlmProfileId} />
              <ConfigLine label="KB" value={countList(config.enabledKnowledgeIds)} />
              <ConfigLine label="MCP" value={countList(config.enabledMcpServerIds)} />
              <ConfigLine label="Files" value={countList(config.fileIds)} />
              <ConfigLine label="Evidence" value={countList(config.evidenceRefs)} />
              {config.evidenceResolution ? (
                <p className="text-[11px] leading-4 text-muted-light">
                  Evidence accepted {config.evidenceResolution.accepted.length}; dropped{" "}
                  {config.evidenceResolution.dropped.length}.
                </p>
              ) : null}
              {config.selectedSkills?.length ? (
                <ChipRow
                  values={config.selectedSkills.map((skill) => skill.name ?? skill.id)}
                />
              ) : null}
            </div>
          ) : (
            <PendingDiagnosticText>
              Waiting for `run.config.resolved` to report the final server-side configuration.
            </PendingDiagnosticText>
          )}
        </DiagnosticCard>

        <DiagnosticCard title="Skill Selection">
          {skillSelection ? (
            <div className="grid gap-2">
              <ConfigLine label="Mode" value={skillSelection.mode ?? "Unlabeled"} />
              {skillSelection.selected.length > 0 ? (
                <ChipRow
                  values={skillSelection.selected.map((skill) => skill.name ?? skill.id)}
                />
              ) : (
                <p className="text-[11px] text-muted-light">No skills selected for this run.</p>
              )}
              {skillSelection.effectiveToolPolicy ? (
                <p className="text-[11px] leading-4 text-muted-light">
                  Tool policy: {summarizeToolPolicy(skillSelection.effectiveToolPolicy)}
                </p>
              ) : null}
            </div>
          ) : (
            <PendingDiagnosticText>
              Waiting for `skill.selection` to report enabled skills and tool policy.
            </PendingDiagnosticText>
          )}
        </DiagnosticCard>

        <DiagnosticCard title="Goal And Context">
          {goal || contextReports.length > 0 ? (
            <div className="grid gap-2">
              {goal ? (
                <div className="rounded-lg border border-border bg-surface px-2.5 py-2">
                  <div className="text-[11px] font-semibold text-foreground">
                    {goal.objective ?? "Untitled goal"}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-muted-light">
                    {goal.status ? <span>Status {goal.status}</span> : null}
                    {goal.source ? <span>Source {goal.source}</span> : null}
                  </div>
                </div>
              ) : null}
              {contextReports.length > 0 ? (
                <ul className="grid gap-1.5">
                  {contextReports.map((report, index) => (
                    <li
                      key={`${report.name}-${report.receivedAt}-${index}`}
                      className="rounded-lg border border-border bg-surface px-2.5 py-2 text-[11px] leading-4 text-muted"
                    >
                      <span className="font-mono text-[10px] text-muted-light">
                        {report.name}
                      </span>
                      <span className="ml-2">{summarizeContextReport(report.value)}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>
          ) : (
            <PendingDiagnosticText>
              Waiting for `goal.updated` and `context.*` to report goal state and context budget.
            </PendingDiagnosticText>
          )}
        </DiagnosticCard>
      </div>
      ) : null}
    </section>
  );
}

function DiagnosticCard({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border bg-surface-subtle p-3">
      <div className={sectionLabelClass}>{title}</div>
      <div className="mt-2">{children}</div>
    </div>
  );
}

function PendingDiagnosticText({ children }: { children: ReactNode }) {
  return <p className="text-[11px] leading-4 text-muted-light">{children}</p>;
}

function ConfigLine({ label, value }: { label: string; value?: string }) {
  return (
    <div className="flex items-center justify-between gap-2 text-[11px]">
      <span className="text-muted-light">{label}</span>
      <span className="truncate font-medium text-muted">{value || "Not set"}</span>
    </div>
  );
}

function ChipRow({ values }: { values: string[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {values.map((value) => (
        <span
          key={value}
          className="rounded-full border border-border bg-surface px-2 py-0.5 text-[10px] font-medium text-muted"
        >
          {value}
        </span>
      ))}
    </div>
  );
}

function countList(values?: readonly unknown[]): string {
  if (!values?.length) return "0 items";
  return `${values.length.toLocaleString()} items`;
}

function formatCount(value: number): string {
  return value.toLocaleString();
}

function summarizeToolPolicy(policy: Record<string, unknown>): string {
  const allowed = Array.isArray(policy.allowedTools)
    ? policy.allowedTools.length
    : Array.isArray(policy.allowed_tools)
      ? policy.allowed_tools.length
      : undefined;
  const denied = Array.isArray(policy.deniedTools)
    ? policy.deniedTools.length
    : Array.isArray(policy.denied_tools)
      ? policy.denied_tools.length
      : undefined;
  return [
    allowed !== undefined ? `allowed ${allowed}` : undefined,
    denied !== undefined ? `denied ${denied}` : undefined,
  ]
    .filter(Boolean)
    .join(" · ") || "reported";
}

function summarizeContextReport(value: unknown): string {
  if (typeof value !== "object" || value === null) return "reported";
  const record = value as Record<string, unknown>;
  const tokenReport =
    typeof record.token_report === "object" && record.token_report !== null
      ? (record.token_report as Record<string, unknown>)
      : record;
  const total =
    typeof tokenReport.total_tokens === "number"
      ? tokenReport.total_tokens
      : typeof record.prompt_tokens === "number"
        ? record.prompt_tokens
        : undefined;
  const budget =
    typeof tokenReport.budget_tokens === "number"
      ? tokenReport.budget_tokens
      : typeof record.remaining_tokens === "number"
        ? record.remaining_tokens
        : undefined;
  return [
    typeof record.model === "string" ? record.model : undefined,
    total !== undefined ? `tokens ${formatCount(total)}` : undefined,
    budget !== undefined ? `budget ${formatCount(budget)}` : undefined,
  ]
    .filter(Boolean)
    .join(" · ") || "reported";
}
