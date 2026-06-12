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
import hashlib
import re
from pathlib import Path

import pandas as pd  # pyright: ignore[reportMissingTypeStubs]

from dataagent.utils.converter.ir_converter_constants import TABLE_FILE_EXTS

# 匹配形如 /tests/e2e/test_data_engineering_workflow.py:72-77 的引用，必须以 / 或者 ~ 开头，行号可选
PATH_LIKE = re.compile(r'(?P<path>(?:/|~)[^\s\'"]+?\.[\w-]+)(?::\d+(?:-\d+)?)?')

# 匹配形如 C:\Users\admin\xxx.csv 或 C:/Users/admin/xxx.csv，可选 :72 或 :72-77
WINDOWS_PATH_LIKE = re.compile(r'(?P<path>[A-Za-z]:(?:\\|/)[^\s\'"]+?\.[\w-]+)(?::\d+(?:-\d+)?)?')


def extract_file_paths_from_query(*, query: str) -> dict[str, list[str]]:
    """
    Extract file paths from query.

    Args:
        query (str): query string

    Returns:
        list[str], list of file paths extracted from query
    """
    candidates: set[str] = set()

    for m in PATH_LIKE.finditer(query):
        candidates.add(m.group("path").rstrip("),.;}]'\""))

    for m in WINDOWS_PATH_LIKE.finditer(query):
        candidates.add(m.group("path").rstrip("),.;}]'\""))

    out_table_paths = []
    out_file_paths = []
    for c in candidates:
        p = Path(c).expanduser()
        if p.suffix and p.exists() and p.is_file():  # keep only file-like candidates
            if p.suffix.lower() in TABLE_FILE_EXTS:
                out_table_paths.append(str(p))
            else:
                out_file_paths.append(str(p))

    return {"Table": sorted(set(out_table_paths)), "File": sorted(set(out_file_paths))}


def load_table(*, path: str, n_rows: int = -1) -> pd.DataFrame:
    """
    Load table from path.

    Args:
        path (str): path of the table, supported file types are .csv and .tsv.
        n_rows (int): number of rows to load, if -1, load all rows

    Returns:
        pd.DataFrame, loaded table of top rows
    """
    if path.endswith(".csv"):
        df = pd.read_csv(path, keep_default_na=False, nrows=n_rows if n_rows >= 0 else None)
        return df.loc[:, ~df.columns.str.match(r"^Unnamed:\s*\d+$")]

    if path.endswith(".tsv"):
        df = pd.read_csv(path, sep="\t", keep_default_na=False, nrows=n_rows if n_rows >= 0 else None)
        return df.loc[:, ~df.columns.str.match(r"^Unnamed:\s*\d+$")]

    raise ValueError(f"Unsupported file type: {path}")


def is_text_file(*, filepath: str, sample_size: int = 8192) -> bool:
    """
    Check if a file is a text file using heuristic method.

    Args:
        filepath (str): path of the file
        sample_size (int): size of the sample to read (Default: `8192`)

    Returns:
        bool, True if the file is a text file, False otherwise
    """
    try:
        with open(filepath, "rb") as f:
            sample = f.read(sample_size)
    except Exception:
        return False

    if not sample:
        return True

    if b"\x00" in sample:  # binary files usually have null bytes
        return False

    try:
        decoded = sample.decode("utf-8", errors="replace")  # not utf-8 text if many characters are replaced
        replacement_count = decoded.count("\ufffd")
        return replacement_count <= max(1, len(decoded) * 0.01)  # at most 1% replacements
    except Exception:
        return False


def load_file(*, filepath: str, max_lines: int = -1) -> str:
    """
    Load file from path.

    Args:
        filepath (str): path of the file
        max_lines (int): number of lines to load, if -1, load all lines

    Returns:
        str, loaded file content
    """
    if is_text_file(filepath=filepath):
        with open(filepath, encoding="utf-8") as f:
            lines = []
            for i, line in enumerate(f):
                if max_lines >= 0 and i >= max_lines:
                    break
                lines.append(line)

        return "\n".join(lines)
    else:
        raise ValueError(f"Unsupported file type: {filepath}.")


def lineage_path_key(*, p: str) -> str:
    """
    Get the key of the lineage path.

    Args:
        p (str): the path to be expanded

    Returns:
        str, the key of the lineage path
    """
    expanded = Path(p).expanduser()
    try:
        return str(expanded.resolve())
    except OSError:
        return str(expanded)


def md5_file(*, p: str, chunk_size: int = 1024 * 1024) -> str:
    """
    Compute md5 of a file with streaming reads.

    Args:
        p (str): the path to the file
        chunk_size (int): the size of the chunk to read (Default: `1024 * 1024`)

    Returns:
        str, the md5 of the file
    """
    h = hashlib.md5()
    with Path(p).open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)

    return h.hexdigest()
