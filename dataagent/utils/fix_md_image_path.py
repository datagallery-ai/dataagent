# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

"""
Fix image paths in a Markdown file so that they are valid paths relative to the
Markdown file's directory. If a referenced image path does not exist, the script
searches the Markdown file's directory and all subdirectories for a file with a
matching basename and updates the path to the shortest relative path.

Supported image syntaxes:
- Inline images: ![alt](path "optional title")
- Reference-style definitions: [id]: path "optional title" (for images)

Unchanged cases:
- URLs starting with http(s)://, ftp://, data: or mailto:
- Anchor references starting with '#'
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from loguru import logger

from dataagent.utils.parsing_utils import parse_destination

INLINE_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
REFERENCE_DEF_PATTERN = re.compile(r"^\s*\[[^\]]+\]:\s+(.+?)\s*$")
SKIP_SCHEMES = (
    "http://",
    "https://",
    "ftp://",
    "data:",
    "mailto:",
    "#",
)


def should_skip(url: str) -> bool:
    """It should be skipped."""
    low = url.strip().lower()
    return low.startswith(SKIP_SCHEMES)


def find_best_match(md_dir: Path, basename: str, verbose: bool = False) -> Path | None:
    """Search md_dir and immediate subdirectories (max depth 1) for a file whose name matches basename."""
    candidates: list[Path] = []

    # Search in md_dir itself (same level)
    for fname in os.listdir(md_dir):
        if fname == basename:
            candidate = md_dir / fname
            if candidate.is_file():
                candidates.append(candidate)

    # Search in immediate subdirectories (next level only)
    try:
        for item in os.listdir(md_dir):
            subdir = md_dir / item
            if subdir.is_dir():
                for fname in os.listdir(subdir):
                    if fname == basename:
                        candidate = subdir / fname
                        if candidate.is_file():
                            candidates.append(candidate)
    except OSError:
        pass  # Skip directories we can't access

    if not candidates:
        return None

    def sort_key(p: Path) -> tuple[int, int, str]:
        rel = p.relative_to(md_dir)
        return (len(str(rel)), len(rel.parts), str(rel))

    candidates.sort(key=sort_key)
    if verbose:
        relative_paths = [str(c.relative_to(md_dir)) for c in candidates]
        logger.trace(f"Found {len(candidates)} candidate(s) for {basename}: {relative_paths}")
    return candidates[0]


def to_relative(md_dir: Path, target: Path) -> str:
    """Convert target path to relative path from md_dir."""
    try:
        rel_path = target.relative_to(md_dir)
    except ValueError:
        # If target is not relative to md_dir, use os.path.relpath as fallback
        rel_path = os.path.relpath(str(target), start=str(md_dir))
    return str(rel_path)


def fix_url(md_dir: Path, url: str, verbose: bool = False) -> str | None:
    """
    Return a fixed relative URL if resolvable, else None.
    Only searches in md_dir and immediate subdirectories (max depth 1).
    """
    if should_skip(url):
        return None

    raw_url = url.strip()
    if raw_url.startswith("<") and raw_url.endswith(">"):
        raw_url = raw_url[1:-1].strip()

    md_dir = md_dir.resolve()
    path = Path(raw_url)

    # Case 1: absolute path - only search by basename in allowed directories
    if path.is_absolute():
        if verbose:
            logger.trace(f"Absolute path {raw_url} - searching for basename {path.name} in allowed directories")
        best = find_best_match(md_dir, path.name, verbose=verbose)
        if best is not None:
            rel = to_relative(md_dir, best)
            if verbose:
                logger.trace(f"Found matching file for {raw_url} -> {rel}")
            return rel
        return None

    # Case 2: relative path - check if it's within allowed scope
    candidate = (md_dir / raw_url).resolve()
    try:
        # Check if the resolved path is still within md_dir or immediate subdirectories
        rel_path = candidate.relative_to(md_dir)
        # Only allow paths with at most 1 level deep (no ".." and max 1 part)
        if len(rel_path.parts) <= 1 and not any(part == ".." for part in rel_path.parts) and candidate.exists():
            rel = to_relative(md_dir, candidate)
            if verbose and rel != raw_url:
                logger.trace(f"Normalized relative path {raw_url} -> {rel}")
            return rel
    except ValueError:
        # Path is outside md_dir, treat as invalid
        pass

    # Case 3: search for file with same basename in allowed directories
    best = find_best_match(md_dir, Path(raw_url).name, verbose=verbose)
    if best is not None:
        rel = to_relative(md_dir, best)
        if verbose:
            logger.trace(f"Found matching file for {raw_url} -> {rel}")
        return rel

    return None


def replace_inline_images(line: str, md_dir: Path, verbose: bool = False) -> tuple[str, int]:
    """Replace inline images"""
    changes = 0

    def repl(match: re.Match) -> str:
        nonlocal changes
        inner = match.group(1)
        url, title = parse_destination(inner)
        fixed = fix_url(md_dir, url, verbose=verbose)
        if fixed is None:
            return match.group(0)
        changes += 1
        if title:
            return match.group(0).replace(inner, f"{fixed} {title}")
        return match.group(0).replace(inner, fixed)

    new_line = INLINE_IMAGE_PATTERN.sub(repl, line)
    return new_line, changes


def replace_reference_defs(line: str, md_dir: Path, verbose: bool = False) -> tuple[str, int]:
    """Replacement of citation definition"""
    m = REFERENCE_DEF_PATTERN.match(line)
    if not m:
        return line, 0
    dest_field = m.group(1)
    url, title = parse_destination(dest_field)
    fixed = fix_url(md_dir, url, verbose=verbose)
    if fixed is None:
        return line, 0
    new_dest = f"{fixed} {title}" if title else fixed
    new_line = line[: m.start(1)] + new_dest + line[m.end(1) :]
    return new_line, 1


def fix_markdown_image_paths(markdown_content: str, md_file_path: str, verbose: bool = False) -> str:
    """Correct the Markdown image path"""
    md_path = Path(md_file_path)
    if not md_path.is_absolute():
        md_path = md_path.resolve()
    md_dir = md_path.parent

    total_changes = 0
    lines = markdown_content.splitlines(keepends=True)
    new_lines: list[str] = []
    for line in lines:
        updated_line, c1 = replace_inline_images(line, md_dir, verbose=verbose)
        if c1 == 0:
            updated_line, c2 = replace_reference_defs(updated_line, md_dir, verbose=verbose)
        else:
            c2 = 0
        total_changes += c1 + c2
        new_lines.append(updated_line)

    if verbose:
        logger.debug(f"Fixed {total_changes} image path(s) in markdown content")

    return "".join(new_lines)


def load_images_as_json(images_path: str) -> str:
    """Load image description configurations and normalize as a JSON array string.

    This helper is used by report generation tools. It supports:
    - A single JSON value: array or object
    - A JSON array file: [..., ...]
    - Legacy JSONL format: one JSON object per line
    """
    try:
        with open(images_path, encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return ""
        try:
            # Prefer single JSON (array or object)
            data = json.loads(raw)
            if isinstance(data, dict) or not isinstance(data, list):
                data = [data]
        except json.JSONDecodeError:
            # Fallback: legacy JSONL (one JSON object per line)
            data = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    data.append(obj)
                except Exception:
                    logger.warning("Failed to parse one image description line, skipped.", exc_info=True)
        return json.dumps(data, ensure_ascii=False)
    except Exception:
        logger.warning(f"Failed to load image descriptions from {images_path}.", exc_info=True)
        return ""


def process_markdown(md_path: Path, dry_run: bool = False, backup: bool = False, verbose: bool = False) -> int:
    """Handle Markdown formatted text"""
    if not md_path.exists():
        logger.debug(f"File not found: {md_path}", file=sys.stderr)
        return 1
    original = md_path.read_text(encoding="utf-8")

    new_content = fix_markdown_image_paths(original, str(md_path), verbose=verbose)

    if dry_run:
        logger.trace(new_content)
        return 0

    if backup:
        backup_path = md_path.with_suffix(md_path.suffix + ".bak")
        backup_path.write_text(original, encoding="utf-8")
        if verbose:
            logger.debug(f"Backup written to: {backup_path}")

    md_path.write_text(new_content, encoding="utf-8")
    if verbose:
        logger.debug(f"Updated {md_path}")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    """main function"""
    parser = argparse.ArgumentParser(description="Fix image paths in a Markdown file.")
    parser.add_argument("md_path", help="Path to the Markdown (.md) file")
    parser.add_argument("--dry-run", action="store_true", help="logger.info result without writing")
    parser.add_argument("--backup", action="store_true", help="Create a .bak backup before writing")
    parser.add_argument("--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args(list(argv) if argv is not None else None)

    md_path = Path(args.md_path)
    if not md_path.is_absolute():
        md_path = md_path.resolve()

    return process_markdown(md_path, dry_run=args.dry_run, backup=args.backup, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
