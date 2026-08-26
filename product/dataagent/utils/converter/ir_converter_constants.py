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
ResultIRConverter 使用的常量与配置。

将表格/脚本/文件类型判定等配置集中管理，便于维护与扩展。
"""

# 结果 dict 中标志内存表格数据的字段组合
TABLE_INDICATOR_KEYS: set[str] = {"columns", "data"}

# 表格 / 数据文件扩展名（含数据库文件）
TABLE_FILE_EXTS: set[str] = {
    ".csv",
    ".xlsx",
    ".xls",
    ".parquet",
    ".tsv",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".duckdb",
}

# ScriptNode script_type 推断映射（从入参 key 推断，也用于判定哪些 key 包含脚本内容）
SCRIPT_TYPE_MAP: dict[str, str] = {
    "sql": "sql",
    "command": "shell",
    "script": "python",
    "code": "python",
}

# 扩展名 -> script_type 映射（从文件扩展名推断，也用于判定是否为脚本文件）
EXT_SCRIPT_TYPE_MAP: dict[str, str] = {
    # SQL
    ".sql": "sql",
    ".ddl": "sql",
    ".dml": "sql",
    ".plsql": "sql",
    ".psql": "sql",
    ".hql": "sql",
    # Python
    ".py": "python",
    ".py3": "python",
    ".pyi": "python",
    ".pyw": "python",
    ".pyx": "cython",
    ".pxd": "cython",
    # Shell
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".fish": "shell",
    ".ksh": "shell",
    ".csh": "shell",
    ".tcsh": "shell",
    ".bat": "batch",
    ".cmd": "batch",
    ".ps1": "powershell",
    ".psm1": "powershell",
    ".psd1": "powershell",
    # JavaScript / TypeScript
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    # C / C++
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".hh": "cpp",
    ".ino": "cpp",
    # C#
    ".cs": "csharp",
    ".csx": "csharp",
    # Java / JVM
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".groovy": "groovy",
    ".gvy": "groovy",
    ".gradle": "groovy",
    ".scala": "scala",
    ".sc": "scala",
    ".clj": "clojure",
    ".cljs": "clojure",
    # Go
    ".go": "go",
    # Rust
    ".rs": "rust",
    # Ruby
    ".rb": "ruby",
    ".rbw": "ruby",
    ".rake": "ruby",
    ".gemspec": "ruby",
    # PHP
    ".php": "php",
    ".php3": "php",
    ".php4": "php",
    ".php5": "php",
    ".phtml": "php",
    # Swift / Objective-C
    ".swift": "swift",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    # Perl
    ".pl": "perl",
    ".pm": "perl",
    ".pod": "perl",
    ".t": "perl",
    # Lua
    ".lua": "lua",
    # R
    ".r": "r",
    ".R": "r",
    ".rmd": "r",
    ".Rmd": "r",
    # Julia
    ".jl": "julia",
    # Dart
    ".dart": "dart",
    # Elixir / Erlang
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hrl": "erlang",
    # Haskell
    ".hs": "haskell",
    ".lhs": "haskell",
    # OCaml / F#
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".fs": "fsharp",
    ".fsx": "fsharp",
    ".fsi": "fsharp",
    # Zig / Nim / V / Crystal
    ".zig": "zig",
    ".nim": "nim",
    ".v": "vlang",
    ".cr": "crystal",
    # Assembly
    ".asm": "assembly",
    ".s": "assembly",
    ".S": "assembly",
    # Build
    ".mk": "make",
    ".cmake": "cmake",
    # IaC / DevOps
    ".tf": "terraform",
    ".hcl": "hcl",
    ".pp": "puppet",
    ".dockerfile": "dockerfile",
    # Web frameworks
    ".vue": "vue",
    ".svelte": "svelte",
    # Other scripting
    ".awk": "awk",
    ".sed": "sed",
    ".tcl": "tcl",
    ".vbs": "vbscript",
    ".ahk": "autohotkey",
    ".m4": "m4",
    ".lisp": "lisp",
    ".el": "elisp",
    ".scm": "scheme",
    ".rkt": "racket",
    ".coffee": "coffeescript",
    ".litcoffee": "coffeescript",
    ".ipynb": "jupyter",
}
