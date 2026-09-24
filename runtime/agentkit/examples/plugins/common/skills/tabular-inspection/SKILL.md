---
name: tabular-inspection
description: Inspect CSV or TSV business files, check their structure and quality, and produce a concise evidence-based summary.
---

# Tabular inspection

1. Identify the exact input file. Use `ls`, `glob` or `read_file` to inspect it;
   ask for clarification if multiple files could match. Never modify the source.
2. Inspect a bounded sample first. Determine encoding, delimiter and whether a header exists;
   do not assume every file is UTF-8 or comma-separated. Report uncertain assumptions.
3. For complete counts, use `execute` with Python's standard-library `csv` module.
   Use the same real input paths as the file tools; quote paths in shell commands.
   Stream rows rather than loading the entire file.
   Do not install pandas just for basic inspection.
4. Report column names, data-row count (excluding the header), blank or duplicate headers,
   missing values and rows whose field count differs from the header. Distinguish sampled
   observations from full-file checks. Missing values mean empty/whitespace-only fields unless
   the user specifies other markers; do not silently count legitimate zero values as missing.
5. Only compute numeric summaries for clearly numeric columns. State how blanks and invalid
   values were handled. For a small explicit numeric list, use `common__summarize_numbers`.
6. Return findings as text unless a file is requested. Save reports in the current session output directory,
   then verify the generated file and include its path. Avoid exposing complete sensitive rows
   when aggregate findings suffice. Report execution failures and incomplete checks honestly.
