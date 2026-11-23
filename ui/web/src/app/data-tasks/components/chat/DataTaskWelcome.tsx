"use client";

import { useT } from "../../../../i18n/locale-context";

function DatabaseIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      className={className}
      aria-hidden
    >
      <ellipse cx="12" cy="5" rx="8" ry="3" />
      <path d="M4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5" />
      <path d="M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6" />
    </svg>
  );
}

function SparkIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      className={className}
      aria-hidden
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.847-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.847a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.847.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 0 0-2.455 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z"
      />
    </svg>
  );
}

function ArrowIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden
    >
      <path d="M3.5 8h9M8.5 4l4 4-4 4" />
    </svg>
  );
}

export function DataTaskWelcomeScreen({
  onUsePrompt,
}: {
  onUsePrompt?: (prompt: string) => void;
}) {
  const t = useT();
  const examplePrompts = [
    {
      title: t("welcome.inspectSchema"),
      description: t("welcome.inspectSchemaDesc"),
      prompt: t("welcome.inspectSchemaPrompt"),
    },
    {
      title: t("welcome.runSql"),
      description: t("welcome.runSqlDesc"),
      prompt: t("welcome.runSqlPrompt"),
    },
    {
      title: t("welcome.analyzeTrends"),
      description: t("welcome.analyzeTrendsDesc"),
      prompt: t("welcome.analyzeTrendsPrompt"),
    },
  ] as const;

  return (
    <div className="mx-auto flex w-full max-w-[792px] flex-1 flex-col justify-center px-4 py-12">
      <div className="mb-5 flex h-9 w-9 items-center justify-center rounded-[10px] bg-primary text-white">
        <DatabaseIcon className="h-[18px] w-[18px]" />
      </div>
      <h2 className="text-[26px] font-semibold leading-tight tracking-[-0.02em] text-foreground">
        {t("welcome.title")}
      </h2>
      <p className="mt-2 max-w-[52ch] text-[14.5px] leading-6 text-muted">
        {t("welcome.subtitle")}
      </p>
      <div className="mt-6 flex flex-wrap gap-x-4 gap-y-1.5 text-[12.5px] text-muted-light">
        <span className="inline-flex items-center gap-1.5">
          <SparkIcon className="h-3.5 w-3.5" />
          {t("welcome.chipSql")}
        </span>
        <span>{t("welcome.chipSchema")}</span>
        <span>{t("welcome.chipTrace")}</span>
      </div>
      <section className="mt-9">
        <h3 className="mb-1.5 text-[12.5px] font-medium text-muted-light">
          {t("welcome.examplesTitle")}
        </h3>
        <div className="grid">
          {examplePrompts.map((item) => (
            <button
              key={item.title}
              type="button"
              onClick={() => onUsePrompt?.(item.prompt)}
              disabled={!onUsePrompt}
              title={onUsePrompt ? t("welcome.useThisPrompt") : undefined}
              className="group flex w-full items-center gap-3 border-t border-border py-3 text-left last:border-b enabled:cursor-pointer disabled:cursor-default"
            >
              <span className="min-w-0 flex-1">
                <span className="block text-[14px] font-medium text-foreground decoration-border-strong underline-offset-[3px] group-enabled:group-hover:underline">
                  {item.title}
                </span>
                <span className="mt-0.5 block truncate text-[13px] text-muted-light">
                  {item.prompt}
                </span>
              </span>
              {onUsePrompt ? (
                <ArrowIcon className="h-4 w-4 shrink-0 text-muted-light transition-[color,transform] duration-150 group-hover:translate-x-0.5 group-hover:text-foreground" />
              ) : null}
            </button>
          ))}
        </div>
      </section>
      <p className="mt-5 text-[12.5px] text-muted-light">
        {t("welcome.footerBeforeAt")}{" "}
        <kbd className="rounded-[5px] bg-surface-hover px-1.5 py-0.5 font-mono text-[11px] text-muted">
          @
        </kbd>{" "}
        {t("welcome.footerAfterAt")}
      </p>
    </div>
  );
}

export function DatasourceChip({ datasourceId }: { datasourceId: string }) {
  return (
    <span className="inline-flex h-6 max-w-[220px] items-center gap-1.5 rounded-md bg-surface-hover px-2 text-[11.5px] text-muted">
      <DatabaseIcon className="h-3.5 w-3.5 shrink-0" />
      <span className="truncate font-mono">{datasourceId}</span>
    </span>
  );
}

export function ChatInitializingState() {
  const t = useT();

  return (
    <div className="grid flex-1 place-items-center px-6">
      <div className="text-center">
        <div className="mx-auto mb-3 h-7 w-7 animate-pulse rounded-full bg-surface-hover" />
        <p className="text-sm text-muted">{t("chat.initializingSession")}</p>
      </div>
    </div>
  );
}
