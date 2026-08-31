import React from 'react';
import { Box, Text } from 'ink';
import type { DataArtifact } from '../state/index.js';
import { TableView } from './components/TableView.js';
import { MarkdownView } from './components/MarkdownView.js';
import { inkColors } from './theme.js';

interface ArtifactCardProps {
  artifact: DataArtifact;
  keyboardActive?: boolean;
  contentWidth?: number | undefined;
  previewRows?: number | undefined;
  previewLoading?: boolean | undefined;
  previewError?: string | undefined;
}

export function isMarkdownFilePath(path: string): boolean {
  return /\.(?:md|markdown|mdx)$/iu.test(path);
}

export function artifactMarkdownContent(artifact: DataArtifact): string | undefined {
  const detail = artifact.detail;
  if (detail?.type === 'report') {
    if (detail.sections.length === 1) {
      return detail.sections[0]?.body;
    }
    return detail.sections
      .map((section) => {
        const body = section.body.trim();
        const heading = section.heading.trim();
        return heading ? `## ${heading}\n\n${body}` : body;
      })
      .filter((section) => section.length > 0)
      .join('\n\n');
  }

  if (detail?.type === 'file' && detail.content && isMarkdownArtifact(artifact)) {
    return detail.content;
  }

  return undefined;
}

export function isMarkdownArtifact(artifact: DataArtifact): boolean {
  const mimeType = artifact.mimeType?.toLowerCase() ?? '';
  const path = artifact.detail?.type === 'file' ? artifact.detail.path : artifact.title;
  return (
    artifact.type === 'report' ||
    artifact.detail?.type === 'report' ||
    mimeType.includes('markdown') ||
    mimeType === 'text/x-markdown' ||
    isMarkdownFilePath(path) ||
    isMarkdownFilePath(artifact.title)
  );
}

export const ArtifactCard: React.FC<ArtifactCardProps> = ({
  artifact,
  keyboardActive = false,
  contentWidth = 80,
  previewRows,
  previewLoading = false,
  previewError,
}) => {
  const markdownContent = artifactMarkdownContent(artifact);
  const previewWidth = Math.max(8, contentWidth - 8);

  // Get artifact icon based on kind
  const getArtifactIcon = (kind: DataArtifact['kind']) => {
    switch (kind) {
      case 'chart':
        return '📊';
      case 'csv':
        return '📋';
      case 'memo':
        return '📝';
      case 'dashboard':
        return '📈';
      case 'file':
        return '📄';
      default:
        return '📄';
    }
  };

  // Get artifact type label
  const getTypeLabel = (type?: DataArtifact['type']) => {
    if (!type) return '';
    switch (type) {
      case 'dataset':
        return '[Dataset]';
      case 'chart':
        return '[Chart]';
      case 'sql':
        return '[SQL]';
      case 'report':
        return '[Report]';
      case 'file':
        return '[File]';
      default:
        return '';
    }
  };

  // Render artifact detail based on type
  const renderDetail = () => {
    if (!artifact.detail) return null;

    switch (artifact.detail.type) {
      case 'sql':
        return (
          <Box flexDirection="column" paddingLeft={2}>
            <Text dimColor>SQL: {artifact.detail.sql.slice(0, 80)}...</Text>
            <Text dimColor>
              Scanned: {artifact.detail.scannedRows} rows, Duration: {artifact.detail.durationMs}ms
            </Text>
          </Box>
        );

      case 'dataset':
        return (
          <Box flexDirection="column" marginTop={1}>
            <TableView
              columns={artifact.detail.columns}
              rows={artifact.detail.rows}
              pageSize={12}
              showPagination={artifact.detail.rows.length > 12}
              enableKeyboardNav={keyboardActive}
            />
          </Box>
        );

      case 'chart':
        return (
          <Box flexDirection="column" paddingLeft={2}>
            <Text dimColor>
              Points: {artifact.detail.points.length}
              {artifact.detail.series?.length ? `, Series: ${artifact.detail.series.length}` : ''}
              {artifact.detail.unit ? `, Unit: ${artifact.detail.unit}` : ''}
              {artifact.detail.chartType ? `, Type: ${artifact.detail.chartType}` : ''}
            </Text>
          </Box>
        );

      case 'report':
        return (
          <Box flexDirection="column" paddingLeft={2}>
            <Text dimColor>
              Sections: {artifact.detail.sections.length}
            </Text>
            {markdownContent && (
              <Box flexDirection="column" marginTop={1}>
                <MarkdownView
                  content={markdownContent}
                  width={previewWidth}
                  maxRows={previewRows}
                  keyboardActive={keyboardActive}
                />
              </Box>
            )}
          </Box>
        );

      case 'file':
        return (
          <Box flexDirection="column" paddingLeft={2}>
            <Text dimColor>Path: {artifact.detail.path}</Text>
            <Text dimColor>
              {artifact.detail.size !== undefined ? `Size: ${artifact.detail.size} bytes` : 'Size: unknown'}
              {artifact.detail.tool ? `, Tool: ${artifact.detail.tool}` : ''}
            </Text>
            {artifact.detail.content && (
              <Box flexDirection="column" marginTop={1}>
                {markdownContent ? (
                  <MarkdownView
                    content={markdownContent}
                    width={previewWidth}
                    maxRows={previewRows}
                    keyboardActive={keyboardActive}
                  />
                ) : (
                  <Text dimColor>{artifact.detail.content.slice(0, 240)}</Text>
                )}
              </Box>
            )}
          </Box>
        );

      default:
        return null;
    }
  };

  const versionLabel = artifact.version?.startsWith('v')
    ? artifact.version
    : artifact.version
      ? `v${artifact.version}`
      : undefined;

  return (
    <Box
      flexDirection="column"
      borderStyle="round"
      borderColor={keyboardActive ? inkColors.accent : inkColors.border}
      paddingX={1}
      marginTop={1}
    >
      {/* Artifact header */}
      <Box>
        <Text>{getArtifactIcon(artifact.kind)} </Text>
        <Text bold>{artifact.title}</Text>
        {artifact.type && (
          <Text dimColor> {getTypeLabel(artifact.type)}</Text>
        )}
      </Box>

      {/* Artifact summary */}
      <Box paddingLeft={2}>
        <Text>{artifact.summary}</Text>
      </Box>

      {(artifact.fileId || artifact.downloadUrl || artifact.previewAvailable) && (
        <Box flexDirection="column" paddingLeft={2}>
          {artifact.fileId && <Text dimColor>file_id: {artifact.fileId}</Text>}
          {artifact.downloadUrl && <Text dimColor>download: {artifact.downloadUrl}</Text>}
          {artifact.previewAvailable && <Text dimColor>preview available</Text>}
        </Box>
      )}

      {/* Artifact details */}
      {renderDetail()}

      {previewLoading && (
        <Box paddingLeft={2}>
          <Text dimColor>Loading preview...</Text>
        </Box>
      )}

      {previewError && (
        <Box paddingLeft={2}>
          <Text color={inkColors.error}>Preview error: {previewError}</Text>
        </Box>
      )}

      {/* Version and timestamp */}
      <Box justifyContent="space-between" marginTop={0}>
        {versionLabel && (
          <Text dimColor>{versionLabel}</Text>
        )}
        {artifact.recordedAtMs && (
          <Text dimColor>
            {new Date(artifact.recordedAtMs).toLocaleTimeString()}
          </Text>
        )}
      </Box>
    </Box>
  );
};
