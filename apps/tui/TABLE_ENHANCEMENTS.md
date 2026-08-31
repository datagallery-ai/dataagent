# TUI Table Output Enhancements

## Current Implementation

The TUI table artifact preview is implemented in `src/ui/components/TableView.tsx` and is used by dataset artifacts through `ArtifactCard.tsx`.

The current version focuses on making SQL/table outputs readable in a terminal without fighting the always-active chat input box.

## Implemented

- Box-drawing table frame with header/body separators.
- CJK-aware width measurement and truncation through `src/ui/text-width.ts`.
- Responsive terminal width fitting with hidden-column notes for narrow terminals.
- Column type inference for text, number, currency, percent, boolean, date, and badge-like categorical fields.
- Type-aware formatting:
  - compact numbers such as `1.2K`, `3.4M`, `5.6B`;
  - percent normalization for rate-like columns;
  - currency symbols when present in data or implied by headers such as `usd`;
  - ISO date/time shortening;
  - boolean and categorical status coloring.
- Type-aware alignment:
  - numeric values right-aligned;
  - booleans centered;
  - text and badges left-aligned.
- Semantic cell colors:
  - positive numeric values green;
  - negative numeric values red;
  - zero/empty values dimmed;
  - success/error/warning/info-like badges color-coded.
- Pagination preview with 12 rows per page.
- Keyboard interaction only for the newest output card:
  - `PageUp` / `PageDown` changes pages;
  - `Ctrl+R` cycles sorting across columns.

## Reference Learnings

### Codex Rust TUI

Useful patterns from `ref/codex-rust-v0.142.5/codex-rs/tui`:

- Keep table rows pre-wrapped to exact terminal cell widths so viewport slicing stays stable.
- Use Unicode box drawing sparingly for structure, and dim borders so data remains the focus.
- Treat wide/narrative cells as a layout problem, not only a truncation problem. The current DataFoundry implementation starts with hidden-column notes; a future version can add Codex-style record/key-value fallback for cramped tables.

### OpenCode TUI

Useful patterns from `ref/opencode-v1.17.14/packages/tui`:

- Keep borders and visual chrome restrained.
- Prefer semantic colors over decorative color blocks.
- Scope keyboard behavior carefully in a TUI with multiple active components.

## Deliberate Constraints

- Global text filtering is not enabled yet. In this app the bottom input box is always active, so ordinary printable keys would also enter the prompt. Filtering should be added only after introducing an explicit output-table focus mode.
- Only the newest output table listens for pagination/sorting keys. This prevents multiple artifact cards from reacting to the same keypress.
- Older table cards render as static previews.

## Future Work

- Add an explicit table focus mode for filtering, row navigation, and column selection.
- Add a Codex-style record view fallback when too many columns contain long text.
- Add artifact export hints for CSV/XLSX once the TUI exposes artifact download actions.
- Let users choose visible columns or pin important dimensions.
