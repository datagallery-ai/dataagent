import { resolveSelectionFocus } from "../apps/api/dist/evidence-reference-context.js";
import { extractEffectiveRunConfig } from "../apps/api/dist/run-input.js";

const preview = {
  columns: ["a", "b", "c"],
  rows: [
    ["1", "2", "3"],
    ["4", "5", "6"],
    ["7", "8", "9"],
  ],
};

let failures = 0;
const check = (name, condition) => {
  if (condition) {
    console.log(`  ok   ${name}`);
  } else {
    failures += 1;
    console.error(`  FAIL ${name}`);
  }
};

// 0) run_config intake must preserve source.selection (partial table cites).
{
  const selection = { mode: "cells", range: { r0: 0, c0: 1, r1: 1, c1: 2 } };
  const config = extractEffectiveRunConfig({
    threadId: "thread-1",
    runId: "run-1",
    messages: [],
    tools: [],
    context: [],
    state: {},
    forwardedProps: {
      run_config: {
        evidenceRefs: [
          {
            id: "artifact:a1:sel:cells:0,1,1,2",
            kind: "table",
            label: "SQL result.csv",
            sessionId: "session-1",
            runId: "run-1",
            source: {
              artifactId: "a1",
              fileId: "file-1",
              selection,
            },
          },
        ],
      },
    },
  });
  const parsed = config.evidenceRefs[0]?.source.selection;
  check("run-input: keeps selection.mode", parsed?.mode === "cells");
  check(
    "run-input: keeps selection.range",
    parsed?.mode !== "text"
      && parsed?.range?.r0 === 0
      && parsed?.range?.c0 === 1
      && parsed?.range?.r1 === 1
      && parsed?.range?.c1 === 2,
  );
  check("run-input: keeps fileId alongside selection", config.evidenceRefs[0]?.source.fileId === "file-1");
}

// 1) cell range slices only the selected sub-table and replaces the full preview.
{
  const focus = resolveSelectionFocus(
    { mode: "cells", range: { r0: 0, c0: 1, r1: 1, c1: 2 } },
    preview,
    6000,
  );
  const text = focus?.lines.join("\n") ?? "";
  check("cells: replaceFullPreview", focus?.replaceFullPreview === true);
  check("cells: selection=B1:C2", text.includes("selection=B1:C2"));
  check("cells: subset header b | c", text.includes("b | c"));
  check("cells: subset row 2 | 3", text.includes("2 | 3"));
  check("cells: excludes column a value 1", !text.includes("1 | 2 | 3"));
  check("cells: keeps full-context note", text.includes("total_rows=3"));
}

// 2) row range keeps all columns for the selected rows.
{
  const focus = resolveSelectionFocus(
    { mode: "rows", range: { r0: 1, c0: 0, r1: 2, c1: 2 } },
    preview,
    6000,
  );
  const text = focus?.lines.join("\n") ?? "";
  check("rows: replaceFullPreview", focus?.replaceFullPreview === true);
  check("rows: selection=rows 2-3", text.includes("selection=rows 2-3"));
  check("rows: includes 4 | 5 | 6", text.includes("4 | 5 | 6"));
  check("rows: excludes header row 1 | 2 | 3", !text.includes("1 | 2 | 3"));
}

// 3) column range narrows to the selected columns and echoes column names.
{
  const focus = resolveSelectionFocus(
    { mode: "cols", range: { r0: 0, c0: 0, r1: 2, c1: 0 }, columns: ["a"] },
    preview,
    6000,
  );
  const text = focus?.lines.join("\n") ?? "";
  check("cols: replaceFullPreview", focus?.replaceFullPreview === true);
  check("cols: selection=cols A-A (a)", text.includes("selection=cols A-A (a)"));
  check("cols: header is a only", text.split("\n").includes("a"));
}

// 4) text selection keeps the whole preview and adds the focused quote.
{
  const focus = resolveSelectionFocus({ mode: "text", quote: "hello world" }, preview, 6000);
  const text = focus?.lines.join("\n") ?? "";
  check("text: keeps full preview", focus?.replaceFullPreview === false);
  check("text: selected_quote", text.includes("selected_quote=hello world"));
}

// 5) out-of-range selection degrades safely to the whole preview.
{
  const focus = resolveSelectionFocus(
    { mode: "cells", range: { r0: 50, c0: 50, r1: 60, c1: 60 } },
    preview,
    6000,
  );
  const text = focus?.lines.join("\n") ?? "";
  check("out-of-range: keeps full preview", focus?.replaceFullPreview === false);
  check("out-of-range: no subset emitted", !text.includes("selected_subset:"));
  check("out-of-range: still notes selection", text.includes("selection="));
}

// 6) selection on a non-table preview degrades to a note only.
{
  const focus = resolveSelectionFocus(
    { mode: "cells", range: { r0: 0, c0: 0, r1: 1, c1: 1 } },
    undefined,
    6000,
  );
  check("no-table: keeps full preview", focus?.replaceFullPreview === false);
  check("no-table: notes selection", (focus?.lines.join("\n") ?? "").includes("selection="));
}

// 7) no selection returns null (whole-object behavior unchanged).
{
  const focus = resolveSelectionFocus(undefined, preview, 6000);
  check("no-selection: null", focus === null);
}

if (failures > 0) {
  console.error(`\nevidence selection slicing: ${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nevidence selection slicing: all checks passed");
