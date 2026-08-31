import React, { useEffect, useMemo, useState } from 'react';
import { Box, Text, useInput } from 'ink';
import {
  artifactDetailFromPreview,
  artifactDetailNeedsPreviewFetch,
  mergeArtifactDetail,
  type DataArtifact,
  type TimelineEvent,
} from '../state/index.js';
import { isMouseInput } from '../input/mouse-wheel.js';
import {
  ArtifactCard,
  artifactMarkdownContent,
  isMarkdownArtifact,
} from './ArtifactCard.js';
import { textWidth, truncateToWidth } from './text-width.js';
import { inkColors, selectionColors } from './theme.js';

interface OutputsViewProps {
  artifacts: DataArtifact[];
  events: TimelineEvent[];
  selectedIndex: number;
  windowStart: number;
  maxVisibleItems: number;
  contentWidth: number;
}

interface OutputsScreenProps {
  artifacts: DataArtifact[];
  events: TimelineEvent[];
  columns?: number | undefined;
  rows?: number | undefined;
  fetchArtifactPreview?: ((id: string) => Promise<unknown>) | undefined;
  fetchArtifactContent?: ((id: string) => Promise<string>) | undefined;
  onCancel: () => void;
}

type PreviewState = {
  detail?: DataArtifact['detail'] | undefined;
  loading?: boolean | undefined;
  error?: string | undefined;
};

const RESERVED_LINES = 5;
const ITEM_HEIGHT = 4;
const SIDEBAR_RESERVED_LINES = 4;
const SIDEBAR_ITEM_HEIGHT = 4;
const MARKDOWN_PREVIEW_TIMEOUT_MS = 5000;

const truncate = (value: string, maxWidth: number): string => {
  const firstLine = value.split(/\r?\n/, 1)[0] ?? '';
  return truncateToWidth(firstLine, Math.max(1, maxWidth), '...');
};

function sourceLabel(artifact: DataArtifact, events: TimelineEvent[]): string {
  if (!artifact.createdByEventId) return '来源步骤未关联';
  const event = events.find((item) => item.id === artifact.createdByEventId);
  if (!event) return `来源 ${artifact.createdByEventId}`;
  return `来自 ${event.title}`;
}

function artifactTypeLabel(artifact: DataArtifact): string {
  switch (artifact.detail?.type ?? artifact.type) {
    case 'dataset':
      return 'Dataset';
    case 'chart':
      return 'Chart';
    case 'sql':
      return 'SQL';
    case 'report':
      return 'Report';
    case 'file':
      return 'File';
    default:
      return artifact.kind;
  }
}

function artifactDetailLabel(artifact: DataArtifact): string | undefined {
  const detail = artifact.detail;
  if (!detail) return undefined;

  switch (detail.type) {
    case 'dataset':
      return `${detail.columns.length.toLocaleString()} 列 x ${detail.rows.length.toLocaleString()} 行`;
    case 'sql':
      return `${detail.scannedRows.toLocaleString()} rows scanned, ${detail.durationMs}ms`;
    case 'chart':
      return [
        `${detail.points.length.toLocaleString()} points`,
        detail.series?.length ? `${detail.series.length.toLocaleString()} series` : undefined,
        detail.chartType,
      ].filter(Boolean).join(', ');
    case 'report':
      return `${detail.sections.length.toLocaleString()} sections`;
    case 'file':
      return [
        detail.path,
        detail.size !== undefined ? `${detail.size.toLocaleString()} bytes` : undefined,
      ].filter(Boolean).join(', ');
    default:
      return undefined;
  }
}

function artifactVersionLabel(artifact: DataArtifact): string | undefined {
  if (!artifact.version) return undefined;
  return artifact.version.startsWith('v') ? artifact.version : `v${artifact.version}`;
}

function artifactTimeLabel(artifact: DataArtifact): string | undefined {
  if (!artifact.recordedAtMs) return undefined;
  return new Date(artifact.recordedAtMs).toLocaleTimeString();
}

function artifactMetadata(artifact: DataArtifact, events: TimelineEvent[]): string {
  return [
    artifactTypeLabel(artifact),
    artifactDetailLabel(artifact),
    artifactVersionLabel(artifact),
    artifactTimeLabel(artifact),
    sourceLabel(artifact, events),
  ].filter(Boolean).join(' - ');
}

function compactArtifactMetadata(artifact: DataArtifact, events: TimelineEvent[]): string {
  const compact = [
    artifactTypeLabel(artifact),
    artifactDetailLabel(artifact),
    artifactVersionLabel(artifact),
    artifactTimeLabel(artifact),
  ].filter(Boolean).join(' - ');

  return compact || sourceLabel(artifact, events);
}

function previewErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function withTimeout<T>(promise: Promise<T>, ms: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timeoutId = setTimeout(() => {
      reject(new Error(`${label} timed out after ${ms}ms`));
    }, ms);

    promise.then(
      (value) => {
        clearTimeout(timeoutId);
        resolve(value);
      },
      (error: unknown) => {
        clearTimeout(timeoutId);
        reject(error);
      },
    );
  });
}

function mergePreviewStateDetail(
  artifact: DataArtifact,
  previewState: PreviewState | undefined,
): DataArtifact['detail'] | undefined {
  if (!previewState?.detail) return artifact.detail;
  return mergeArtifactDetail(artifact.detail, previewState.detail);
}

function withPreviewState(
  artifact: DataArtifact | undefined,
  previewState: PreviewState | undefined,
): DataArtifact | undefined {
  if (!artifact) return undefined;
  const detail = mergePreviewStateDetail(artifact, previewState);
  return { ...artifact, detail };
}

function detailFooterText(
  artifact: DataArtifact | undefined,
  previewState: PreviewState | undefined,
): string {
  if (!artifact) return 'Esc/Backspace list - q close';
  if (previewState?.loading) {
    return 'Esc/Backspace list - q close - loading preview';
  }
  if (previewState?.error) {
    return 'Esc/Backspace list - q close';
  }
  if (artifactMarkdownContent(artifact) || isMarkdownArtifact(artifact)) {
    return 'Esc/Backspace list - q close - Up/Down/PageUp/PageDown scroll';
  }
  if (artifact.detail?.type === 'dataset') {
    return 'Esc/Backspace list - q close - PageUp/PageDown table';
  }
  return 'Esc/Backspace list - q close';
}

export const OutputsView: React.FC<OutputsViewProps> = ({
  artifacts,
  events,
  selectedIndex,
  windowStart,
  maxVisibleItems,
  contentWidth,
}) => {
  const visibleArtifacts = artifacts.slice(windowStart, windowStart + maxVisibleItems);
  const showScrollUp = windowStart > 0;
  const showScrollDown = windowStart + maxVisibleItems < artifacts.length;

  return (
    <Box flexDirection="column" flexGrow={1} paddingX={1} overflow="hidden">
      {artifacts.length === 0 ? (
        <Box flexDirection="column" paddingY={2}>
          <Text color={selectionColors.disabled} wrap="truncate-end">
            {truncate('暂无产出。', contentWidth)}
          </Text>
          <Text color={selectionColors.description} wrap="truncate-end">
            {truncate('Agent 生成 SQL 结果、图表、报告或文件后会显示在这里。', contentWidth)}
          </Text>
        </Box>
      ) : (
        <Box flexDirection="column" flexGrow={1} overflow="hidden">
          <Box marginBottom={1}>
            <Text color={selectionColors.description} wrap="truncate-end">
              {truncate('最新产出排在前面。上下选择，Enter 查看详情。', contentWidth)}
            </Text>
          </Box>
          {visibleArtifacts.map((artifact, index) => {
            const absoluteIndex = windowStart + index;
            const selected = absoluteIndex === selectedIndex;
            const isFirst = index === 0;
            const isLast = index === visibleArtifacts.length - 1;
            const prefix = selected
              ? '› '
              : isFirst && showScrollUp
                ? '^ '
                : isLast && showScrollDown
                  ? 'v '
                  : '  ';
            const titleWidth = Math.max(1, contentWidth - 2);
            const summaryWidth = Math.max(1, contentWidth - 2);
            const metadataWidth = Math.max(1, contentWidth - 2);
            const typeLabel = ` [${artifactTypeLabel(artifact)}]`;
            const title = truncate(
              artifact.title,
              Math.max(1, titleWidth - textWidth(typeLabel)),
            );

            return (
              <Box
                key={artifact.id}
                flexDirection="column"
                marginBottom={isLast ? 0 : 1}
                backgroundColor={selected
                  ? selectionColors.selectedBackground
                  : selectionColors.background}
              >
                <Box>
                  <Text color={selected ? selectionColors.accent : selectionColors.disabled}>
                    {prefix}
                  </Text>
                  <Text
                    color={selected ? selectionColors.selectedTitle : selectionColors.title}
                    bold={selected}
                    wrap="truncate-end"
                  >
                    {title}
                  </Text>
                  <Text
                    color={selected
                      ? selectionColors.selectedDescription
                      : selectionColors.description}
                    wrap="truncate-end"
                  >
                    {truncate(typeLabel, Math.max(1, titleWidth - textWidth(title)))}
                  </Text>
                </Box>
                <Box paddingLeft={2}>
                  <Text
                    color={selected
                      ? selectionColors.selectedDescription
                      : selectionColors.description}
                    wrap="truncate-end"
                  >
                    {truncate(artifact.summary, summaryWidth)}
                  </Text>
                </Box>
                <Box paddingLeft={2}>
                  <Text
                    color={selected
                      ? selectionColors.selectedDescription
                      : selectionColors.disabled}
                    wrap="truncate-end"
                  >
                    {truncate(artifactMetadata(artifact, events), metadataWidth)}
                  </Text>
                </Box>
              </Box>
            );
          })}
        </Box>
      )}
    </Box>
  );
};

export const OutputsSidebar: React.FC<{
  artifacts: DataArtifact[];
  events: TimelineEvent[];
  columns?: number | undefined;
  rows?: number | undefined;
}> = ({
  artifacts,
  events,
  columns = 42,
  rows = 24,
}) => {
  const panelWidth = Math.max(24, Math.floor(columns));
  const panelHeight = Math.max(0, Math.floor(rows));
  const contentWidth = Math.max(10, panelWidth - 4);
  const separatorWidth = Math.max(0, panelWidth - 2);
  const maxVisibleItems = Math.max(
    1,
    Math.floor((panelHeight - SIDEBAR_RESERVED_LINES) / SIDEBAR_ITEM_HEIGHT),
  );
  const visibleArtifacts = artifacts.slice(0, maxVisibleItems);
  const hiddenCount = Math.max(0, artifacts.length - visibleArtifacts.length);
  const headerSuffix = ` ${artifacts.length}`;
  const headerTitle = truncate(
    'Outputs',
    Math.max(1, contentWidth - textWidth(headerSuffix)),
  );
  const footerText = hiddenCount > 0
    ? `+${hiddenCount} more - open /outputs`
    : 'Open /outputs for details';

  if (panelHeight <= 0) {
    return <Box width={panelWidth} flexShrink={0} />;
  }

  return (
    <Box
      flexDirection="column"
      width={panelWidth}
      height={panelHeight}
      flexShrink={0}
      borderStyle="single"
      borderColor={inkColors.border}
      overflow="hidden"
    >
      <Box paddingX={1}>
        <Text bold color={inkColors.accent} wrap="truncate-end">{headerTitle}</Text>
        <Text dimColor wrap="truncate-end">
          {truncate(headerSuffix, Math.max(1, contentWidth - textWidth(headerTitle)))}
        </Text>
      </Box>

      <Box>
        <Text color={inkColors.border}>{'-'.repeat(separatorWidth)}</Text>
      </Box>

      <Box flexDirection="column" flexGrow={1} paddingX={1} overflow="hidden">
        {artifacts.length === 0 ? (
          <Box flexDirection="column" paddingY={1}>
            <Text dimColor wrap="truncate-end">
              {truncate('暂无产出。', contentWidth)}
            </Text>
          </Box>
        ) : (
          visibleArtifacts.map((artifact, index) => {
            const isLatest = index === 0;
            const indexLabel = `${index + 1}. `;
            const typeLabel = ` ${artifactTypeLabel(artifact)}`;
            const title = truncate(
              artifact.title,
              Math.max(
                1,
                contentWidth - textWidth(indexLabel) - textWidth(typeLabel),
              ),
            );
            const summary = truncate(artifact.summary, Math.max(1, contentWidth - 2));
            const metadata = truncate(
              compactArtifactMetadata(artifact, events),
              Math.max(1, contentWidth - 2),
            );
            const isLast = index === visibleArtifacts.length - 1;

            return (
              <Box
                key={artifact.id}
                flexDirection="column"
                marginBottom={isLast ? 0 : 1}
              >
                <Box>
                  <Text color={isLatest ? inkColors.accent : inkColors.muted}>
                    {indexLabel}
                  </Text>
                  <Text color={isLatest ? inkColors.accent : inkColors.text} wrap="truncate-end">
                    {title}
                  </Text>
                  <Text dimColor wrap="truncate-end">
                    {truncate(typeLabel, Math.max(1, contentWidth - textWidth(indexLabel) - textWidth(title)))}
                  </Text>
                </Box>
                <Box paddingLeft={2}>
                  <Text dimColor wrap="truncate-end">{summary}</Text>
                </Box>
                <Box paddingLeft={2}>
                  <Text dimColor wrap="truncate-end">{metadata}</Text>
                </Box>
              </Box>
            );
          })
        )}
      </Box>

      <Box>
        <Text color={inkColors.border}>{'-'.repeat(separatorWidth)}</Text>
      </Box>

      <Box paddingX={1}>
        <Text dimColor wrap="truncate-end">
          {truncate(footerText, contentWidth)}
        </Text>
      </Box>
    </Box>
  );
};

const OutputsDetailView: React.FC<{
  artifact: DataArtifact;
  events: TimelineEvent[];
  index: number;
  contentWidth: number;
  previewRows: number;
  previewLoading?: boolean | undefined;
  previewError?: string | undefined;
}> = ({
  artifact,
  events,
  index,
  contentWidth,
  previewRows,
  previewLoading,
  previewError,
}) => {
  return (
    <Box flexDirection="column" flexGrow={1} paddingX={1} overflow="hidden">
      <Box>
        <Text dimColor>#{index + 1} </Text>
        <Text color={selectionColors.accent} wrap="truncate-end">
          {truncate(sourceLabel(artifact, events), Math.max(1, contentWidth - 4))}
        </Text>
      </Box>
      <Box flexDirection="column" flexGrow={1} overflow="hidden">
        <ArtifactCard
          artifact={artifact}
          keyboardActive
          contentWidth={contentWidth}
          previewRows={previewRows}
          previewLoading={previewLoading}
          previewError={previewError}
        />
      </Box>
    </Box>
  );
};

export const OutputsScreen: React.FC<OutputsScreenProps> = ({
  artifacts,
  events,
  columns = 100,
  rows = 40,
  fetchArtifactPreview,
  fetchArtifactContent,
  onCancel,
}) => {
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(
    () => artifacts[0]?.id ?? null,
  );
  const [detailOpen, setDetailOpen] = useState(false);
  const [previewStateById, setPreviewStateById] = useState<Record<string, PreviewState>>({});
  const panelWidth = Math.max(24, columns);
  const panelHeight = Math.max(8, rows - 1);
  const contentWidth = Math.max(10, panelWidth - 4);
  const separatorWidth = Math.max(0, panelWidth - 2);
  const previewRows = Math.max(4, panelHeight - 13);
  const maxVisibleItems = Math.max(
    1,
    Math.floor((panelHeight - RESERVED_LINES) / ITEM_HEIGHT),
  );
  const selectedIndex = useMemo(() => {
    if (artifacts.length === 0) return -1;
    const index = artifacts.findIndex((artifact) => artifact.id === selectedArtifactId);
    return index >= 0 ? index : 0;
  }, [artifacts, selectedArtifactId]);
  const selectedBaseArtifact = selectedIndex >= 0 ? artifacts[selectedIndex] : undefined;
  const selectedPreviewState = selectedBaseArtifact
    ? previewStateById[selectedBaseArtifact.id]
    : undefined;
  const selectedArtifact = withPreviewState(selectedBaseArtifact, selectedPreviewState);
  const windowStart = Math.max(
    0,
    Math.min(
      selectedIndex - Math.floor(maxVisibleItems / 2),
      Math.max(0, artifacts.length - maxVisibleItems),
    ),
  );
  const headerSuffix = detailOpen && selectedArtifact
    ? ` / ${selectedIndex + 1}`
    : ` (${artifacts.length})`;
  const headerTitle = truncate(
    'Outputs',
    Math.max(1, contentWidth - textWidth(headerSuffix)),
  );
  const footerText = detailOpen
    ? detailFooterText(selectedArtifact, selectedPreviewState)
    : 'Up/Down/j/k navigate - Enter view - Esc/q close';

  useEffect(() => {
    if (artifacts.length === 0) {
      setSelectedArtifactId(null);
      setDetailOpen(false);
      return;
    }

    if (!selectedArtifactId || !artifacts.some((artifact) => artifact.id === selectedArtifactId)) {
      setSelectedArtifactId(artifacts[0]?.id ?? null);
    }
  }, [artifacts, selectedArtifactId]);

  useEffect(() => {
    if (!detailOpen || !selectedBaseArtifact || !fetchArtifactPreview) return;

    const previewState = previewStateById[selectedBaseArtifact.id];
    if (previewState?.loading || previewState?.detail || previewState?.error) return;

    const currentDetail = mergePreviewStateDetail(selectedBaseArtifact, previewState);
    if (!artifactDetailNeedsPreviewFetch(selectedBaseArtifact, currentDetail)) return;

    let cancelled = false;
    const artifactId = selectedBaseArtifact.id;

    setPreviewStateById((current) => {
      const existing = current[artifactId];
      if (existing?.loading || existing?.detail || existing?.error) return current;
      return {
        ...current,
        [artifactId]: { ...existing, loading: true, error: undefined },
      };
    });

    const markdownContentFallbackAvailable = Boolean(
      fetchArtifactContent
      && isMarkdownArtifact(selectedBaseArtifact)
      && (selectedBaseArtifact.fileId || selectedBaseArtifact.downloadUrl),
    );

    const loadPreviewDetail = async (): Promise<DataArtifact['detail'] | undefined> => {
      try {
        const preview = markdownContentFallbackAvailable
          ? await withTimeout(
              fetchArtifactPreview(artifactId),
              MARKDOWN_PREVIEW_TIMEOUT_MS,
              'Artifact preview',
            )
          : await fetchArtifactPreview(artifactId);
        const detail = artifactDetailFromPreview(selectedBaseArtifact, preview);
        if (detail || !markdownContentFallbackAvailable || !fetchArtifactContent) {
          return detail;
        }
      } catch (error) {
        if (!markdownContentFallbackAvailable || !fetchArtifactContent) {
          throw error;
        }
      }

      const content = await withTimeout(
        fetchArtifactContent(artifactId),
        MARKDOWN_PREVIEW_TIMEOUT_MS,
        'Artifact content',
      );
      const fallbackPreview = selectedBaseArtifact.type === 'file'
        ? {
            type: 'file',
            path: selectedBaseArtifact.title,
            content,
          }
        : {
            type: 'markdown',
            path: selectedBaseArtifact.title,
            content,
          };
      return artifactDetailFromPreview(selectedBaseArtifact, fallbackPreview);
    };

    loadPreviewDetail()
      .then((detail) => {
        if (cancelled) return;
        setPreviewStateById((current) => ({
          ...current,
          [artifactId]: detail
            ? { detail, loading: false }
            : {
                loading: false,
                error: 'Preview data is empty or unsupported.',
              },
        }));
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setPreviewStateById((current) => ({
          ...current,
          [artifactId]: {
            loading: false,
            error: previewErrorMessage(error),
          },
        }));
      });

    return () => {
      cancelled = true;
    };
  // Do not depend on previewStateById here. This effect sets the selected
  // artifact to loading; if that state update retriggers cleanup, the in-flight
  // preview promise is marked cancelled and its result is discarded.
  }, [
    detailOpen,
    fetchArtifactContent,
    fetchArtifactPreview,
    selectedBaseArtifact,
  ]);

  useInput((input, key) => {
    if (isMouseInput(input)) {
      return;
    }

    if (detailOpen) {
      if (input === 'q') {
        onCancel();
        return;
      }
      if (key.escape || key.backspace || key.delete) {
        setDetailOpen(false);
      }
      return;
    }

    if (key.escape || input === 'q') {
      onCancel();
      return;
    }

    if (key.upArrow || (key.ctrl && input === 'p') || input === 'k') {
      if (artifacts.length === 0) return;
      const nextIndex = Math.max(0, selectedIndex - 1);
      setSelectedArtifactId(artifacts[nextIndex]?.id ?? null);
      return;
    }

    if (key.downArrow || (key.ctrl && input === 'n') || input === 'j') {
      if (artifacts.length === 0) return;
      const nextIndex = Math.min(artifacts.length - 1, selectedIndex + 1);
      setSelectedArtifactId(artifacts[nextIndex]?.id ?? null);
      return;
    }

    if (key.return && selectedArtifact) {
      setDetailOpen(true);
    }
  });

  return (
    <Box
      flexDirection="column"
      width={panelWidth}
      height={panelHeight}
      overflow="hidden"
    >
      <Box
        flexDirection="column"
        borderStyle="single"
        borderColor={selectionColors.border}
        backgroundColor={selectionColors.background}
        width={panelWidth}
        height={panelHeight}
        overflow="hidden"
      >
        <Box paddingX={1}>
          <Text bold color={selectionColors.heading} wrap="truncate-end">{headerTitle}</Text>
          <Text color={selectionColors.description} wrap="truncate-end">
            {truncate(headerSuffix, Math.max(1, contentWidth - textWidth(headerTitle)))}
          </Text>
        </Box>

        <Box>
          <Text color={selectionColors.border}>{'-'.repeat(separatorWidth)}</Text>
        </Box>

        <Box flexDirection="column" flexGrow={1} overflow="hidden">
          {detailOpen && selectedArtifact ? (
            <OutputsDetailView
              artifact={selectedArtifact}
              events={events}
              index={selectedIndex}
              contentWidth={contentWidth}
              previewRows={previewRows}
              previewLoading={selectedPreviewState?.loading}
              previewError={selectedPreviewState?.error}
            />
          ) : (
            <OutputsView
              artifacts={artifacts}
              events={events}
              selectedIndex={selectedIndex}
              windowStart={windowStart}
              maxVisibleItems={maxVisibleItems}
              contentWidth={contentWidth}
            />
          )}
        </Box>

        <Box>
          <Text color={selectionColors.border}>{'-'.repeat(separatorWidth)}</Text>
        </Box>

        <Box paddingX={1}>
          <Text color={selectionColors.disabled} wrap="truncate-end">
            {truncate(footerText, contentWidth)}
          </Text>
        </Box>
      </Box>
    </Box>
  );
};
