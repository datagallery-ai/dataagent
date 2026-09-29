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
# 本文件中的模型可见文案常量原样取自 deepagents，按 MIT 许可保留其署名：
#
#     deepagents — Copyright (c) LangChain, Inc. — MIT License
#
# 这些文案是行为契约（模型读到什么字就按什么字行事），不得改写。
# ----------------------------------------------------------------------------
"""用子类提供的 ``execute`` 在沙箱里完成文件操作。

远程脚本是模块级常量，逻辑不要去 import ``filesystem``。``adelete`` /
``aexecute`` / ``aupload_files`` / ``adownload_files`` 保持基类。
``agrep`` / ``aglob`` 的超时只包在 ``asyncio.wait_for`` 外面。
"""

from __future__ import annotations

import abc
import asyncio
import base64
import json
import logging
import os
import shlex
from typing import Any, Final

from . import protocol as contracts
from . import utils as text_tools

ASYNC_GLOB_TIMEOUT = contracts.ASYNC_GLOB_TIMEOUT
ASYNC_GREP_TIMEOUT = contracts.ASYNC_GREP_TIMEOUT
execute_accepts_timeout = contracts.execute_accepts_timeout
EMPTY_OLD_STRING_ERROR = text_tools.EMPTY_OLD_STRING_ERROR
normalize_read_bounds = text_tools.normalize_read_bounds
_get_backend_read_file_type = text_tools._get_backend_read_file_type

DeleteResult = contracts.DeleteResult
EditResult = contracts.EditResult
ExecuteOffloadResult = contracts.ExecuteOffloadResult
ExecuteResponse = contracts.ExecuteResponse
FileData = contracts.FileData
FileDownloadResponse = contracts.FileDownloadResponse
FileUploadResponse = contracts.FileUploadResponse
GlobResult = contracts.GlobResult
GrepResult = contracts.GrepResult
LsResult = contracts.LsResult
ReadResult = contracts.ReadResult
SandboxBackendProtocol = contracts.SandboxBackendProtocol
WriteResult = contracts.WriteResult

logger = logging.getLogger(__name__)

MAX_BINARY_BYTES = 500 * 1024
MAX_OUTPUT_BYTES = 500 * 1024
_EDIT_INLINE_MAX_BYTES = 50_000
_EXECUTE_CAPTURE_SENTINEL = "__DEEPAGENTS_EXEC_META__"
_EXECUTE_CAPTURE_HEAD_LINES = 5
_EXECUTE_CAPTURE_TAIL_LINES = 5
_EXECUTE_CAPTURE_HEAD_BYTES = 2000
_EXECUTE_CAPTURE_TAIL_BYTES = 2000
_EXECUTE_CAPTURE_MAX_BYTES = 10 * 1024 * 1024

_GLOB_COMMAND_TEMPLATE = """python3 -c "
import fnmatch
import os
import json
import base64
import time

# Decode base64-encoded parameters
path = base64.b64decode('{path_b64}').decode('utf-8')
pattern = base64.b64decode('{pattern_b64}').decode('utf-8')

# Bounds on a model-supplied pattern over an untrusted tree. Exceeding any of
# them sets the truncated flag on the result rather than failing. TIME_BUDGET is
# the sandbox-side analogue of _DEFAULT_GLOB_TIMEOUT in filesystem.py and is
# deliberately the same 5s; the outer round-trip is bounded separately by
# ASYNC_GLOB_TIMEOUT. (No backticks: this comment runs through sh.)
MAX_EXPANSIONS = 1000
MAX_MATCHES = 10000
TIME_BUDGET = 5.0


def _find_group_end(pat, start):
    # Index of the '}}' closing the group opened at 'start', or -1 if unbalanced.
    depth = 0
    for index in range(start, len(pat)):
        if pat[index] == '{{':
            depth += 1
        elif pat[index] == '}}':
            depth -= 1
            if depth == 0:
                return index
    return -1


def _split_alternatives(body):
    # Split on top-level commas only, so nested groups survive intact.
    parts = []
    depth = 0
    current = ''
    for ch in body:
        if ch == '{{':
            depth += 1
            current += ch
        elif ch == '}}':
            depth -= 1
            current += ch
        elif ch == ',' and depth == 0:
            parts.append(current)
            current = ''
        else:
            current += ch
    parts.append(current)
    return parts


def _brace_expand(pat):
    # pattern is model/user supplied, so expansion must be bounded: the full
    # Cartesian product is materialized in memory, and 2**n groups would
    # otherwise hang or OOM the sandbox before the walk starts. Returns None
    # past the budget, mirroring the expansion limit wcmatch enforces in
    # compile_grep_include_glob. Nested groups expand like wcmatch's BRACE.
    start = pat.find('{{')
    if start < 0:
        return [pat]
    end = _find_group_end(pat, start)
    if end < 0:
        return [pat]
    prefix, body, suffix = pat[:start], pat[start + 1 : end], pat[end + 1 :]
    parts = _split_alternatives(body)
    if len(parts) < 2:
        # A single-element group is literal, but the rest may still expand.
        tails = _brace_expand(suffix)
        if tails is None:
            return None
        return [prefix + '{{' + body + '}}' + tail for tail in tails]
    out = []
    for part in parts:
        tails = _brace_expand(part + suffix)
        if tails is None:
            return None
        for tail in tails:
            out.append(prefix + tail)
            if len(out) > MAX_EXPANSIONS:
                return None
    return out


def _normalize_classes(pat):
    # fnmatch reads a leading '^' in a bracket expression as a literal, while
    # wcmatch (and bash/ripgrep) read it as negation. Without this rewrite
    # '[^a]*.py' is inverted on the sandbox: it returns exactly the files the
    # caller meant to exclude.
    out = ''
    index = 0
    while index < len(pat):
        if pat[index] != '[':
            out += pat[index]
            index += 1
            continue
        # Start at index + 2 so a literal ']' first in the set is kept ('[]]').
        close = pat.find(']', index + 2)
        if close < 0:
            out += pat[index:]
            break
        body = pat[index + 1 : close]
        if body.startswith('^'):
            body = '!' + body[1:]
        out += '[' + body + ']'
        index = close + 1
    return out


def _basename_match(name, candidates):
    for candidate in candidates:
        # No DOTMATCH: leading-dot basenames need an explicit leading '.' pattern.
        if name.startswith('.') and not candidate.startswith('.'):
            continue
        # fnmatchcase, not fnmatch: fnmatch applies os.path.normcase, which would
        # make matching case-insensitive on a non-POSIX host. wcmatch is always
        # case-sensitive here.
        if fnmatch.fnmatchcase(name, candidate):
            return True
    return False


def _parts_match(rel_parts, pat_parts):
    # Memoized on (path index, pattern index): '**' otherwise backtracks
    # exponentially, so '**/*/**/*'-shaped patterns hang the sandbox.
    cache = {{}}

    def match_from(ri, pi):
        key = (ri, pi)
        if key in cache:
            return cache[key]
        result = _compute(ri, pi)
        cache[key] = result
        return result

    def _compute(ri, pi):
        while pi < len(pat_parts):
            if pat_parts[pi] == '**':
                while pi < len(pat_parts) and pat_parts[pi] == '**':
                    pi += 1
                if pi == len(pat_parts):
                    # A slash before a trailing ** requires at least one
                    # descendant; a.py/** must not match the file a.py.
                    return ri < len(rel_parts) and all(not part.startswith('.') for part in rel_parts[ri:])
                while ri <= len(rel_parts):
                    if match_from(ri, pi):
                        return True
                    if ri == len(rel_parts):
                        break
                    # ** without DOTMATCH does not traverse leading-dot segments.
                    if rel_parts[ri].startswith('.'):
                        return False
                    ri += 1
                return False
            if ri >= len(rel_parts):
                return False
            name = rel_parts[ri]
            seg = pat_parts[pi]
            if name.startswith('.') and not seg.startswith('.'):
                return False
            if not fnmatch.fnmatchcase(name, seg):
                return False
            ri += 1
            pi += 1
        return ri == len(rel_parts)

    return match_from(0, 0)


def _path_match(rel, candidates):
    rel_parts = [] if rel in ('', '.') else [seg for seg in rel.split('/') if seg]
    for candidate in candidates:
        relative_candidate = candidate.lstrip('/')
        segments = relative_candidate.split('/')
        # Drop empty segments so 'a//b.py' matches 'a/b.py', as wcmatch does.
        pat_parts = [seg for seg in segments if seg]
        # A trailing slash means directory-only, and only regular files are
        # emitted -- except after '**', which absorbs it.
        if len(segments) > 1 and segments[-1] == '' and (not pat_parts or pat_parts[-1] != '**'):
            continue
        if _parts_match(rel_parts, pat_parts):
            return True
    return False


def _include_match(rel, pat, candidates):
    # Shared backend contract (same idea as compile_grep_include_glob):
    # - no '/' -> basename at any depth (including under hidden dirs)
    # - with '/' -> path-relative, ** supported, leading '/' anchors after lstrip
    if '/' not in pat:
        name = rel.rsplit('/', 1)[-1]
        return _basename_match(name, candidates)
    return _path_match(rel, candidates)


walk_errors = []

# Pseudo-filesystems are effectively infinite and never hold user files. A bare
# pattern is basename-at-any-depth, so a search rooted at '/' would otherwise
# burn the entire time budget in /proc and return an arbitrary prefix.
PRUNE_AT_ROOT = ('proc', 'sys', 'dev')


def _on_walk_error(err):
    # Keep the failing path, not just the exception class: 'PermissionError' x40
    # cannot distinguish one chronically unreadable mount from an unreadable tree.
    walk_errors.append(type(err).__name__ + ':' + str(getattr(err, 'filename', '?')))


def _emit(matches, truncated):
    for item in sorted(matches):
        print(json.dumps({{'path': item, 'is_dir': False}}))
    if walk_errors:
        print(json.dumps({{
            'warning': 'walk_errors',
            'count': len(walk_errors),
            'sample': walk_errors[:5],
        }}))
    if truncated:
        print(json.dumps({{'warning': 'truncated'}}))


matches = []
truncated = False
# Prologue: everything that can fail before any match exists. Kept in its own
# try so its handlers only ever fire when there is genuinely nothing to report --
# a failure raised from inside the walk below must not be reported as an
# inaccessible search root while discarding thousands of good matches.
ready = False
try:
    real_root = os.path.realpath(path)
    # os.path.realpath('/') is '/', so a naive real_root + os.sep is '//', which
    # no absolute path starts with. Normalize, or a search rooted at '/' (the
    # default when no path is passed) silently drops every match.
    root_prefix = real_root if real_root.endswith(os.sep) else real_root + os.sep
    os.chdir(path)
    if any(seg == '..' for seg in pattern.replace(chr(92), '/').split('/')):
        print(json.dumps({{'error': 'invalid_pattern'}}))
    else:
        expanded = _brace_expand(pattern)
        if expanded is None:
            print(json.dumps({{'error': 'pattern_too_broad'}}))
        else:
            candidates = [_normalize_classes(item) for item in expanded]
            ready = True
except FileNotFoundError:
    print(json.dumps({{'error': 'path_not_found'}}))
except NotADirectoryError:
    print(json.dumps({{'error': 'not_a_directory'}}))
except PermissionError:
    print(json.dumps({{'error': 'permission_denied'}}))
except Exception as exc:
    # Without this, any other failure reaches stdout as a traceback that the
    # host parser cannot read, and the caller sees a successful empty search.
    # Carry the message, bounded: 'internal_error: KeyError' alone cannot
    # distinguish a pattern-parser bug from a surrogate in a filename.
    print(json.dumps({{'error': 'internal_error: ' + type(exc).__name__ + ': ' + str(exc)[:200]}}))

if ready:
    deadline = time.monotonic() + TIME_BUDGET
    at_root = real_root == os.sep
    try:
        # os.walk includes hidden directories; matching rules still exclude
        # leading-dot basenames unless the pattern is explicit (no DOTMATCH).
        # onerror is required: os.walk otherwise discards unreadable subtrees
        # silently, shrinking the result set with no signal to the caller.
        for dirpath, dirnames, filenames in os.walk('.', onerror=_on_walk_error):
            if truncated:
                break
            if dirpath == '.' and at_root:
                dirnames[:] = [d for d in dirnames if d not in PRUNE_AT_ROOT]
            if time.monotonic() > deadline:
                truncated = True
                break
            for name in filenames:
                if time.monotonic() > deadline or len(matches) >= MAX_MATCHES:
                    truncated = True
                    break
                full = name if dirpath == '.' else os.path.join(dirpath, name)
                rel = full.replace(chr(92), '/')
                if rel.startswith('./'):
                    rel = rel[2:]
                if not _include_match(rel, pattern, candidates):
                    continue
                candidate = os.path.realpath(full)
                if candidate != real_root and not candidate.startswith(root_prefix):
                    continue
                # Regular files only, mirroring FilesystemBackend.glob's
                # is_file() filter; also drops broken symlinks.
                if not os.path.isfile(candidate):
                    continue
                matches.append(rel)
    except Exception as exc:
        # A failure mid-walk (a symlink racing a deletion, an unreadable entry
        # os.walk raises rather than routing to onerror) must not throw away the
        # matches already found. Record it as a walk error and emit the partial
        # set -- valid but incomplete, which is exactly what 'walk_errors' means.
        walk_errors.append(type(exc).__name__ + ':' + str(exc)[:100])
    _emit(matches, truncated)

" 2>&1"""

_GREP_PATH_GLOB_TEMPLATE = """python3 -c "
import glob, os, base64, sys

search_path = base64.b64decode('{path_b64}').decode('utf-8')
glob_pat = base64.b64decode('{glob_b64}').decode('utf-8')
pattern = base64.b64decode('{pattern_b64}').decode('utf-8')
max_count = {max_count}
match_count = 0

# When the search path is a directory, chdir to it so glob patterns
# resolve relative to it. When it is a single file, search it directly
# (glob filtering is irrelevant for a single-file search).
if os.path.isdir(search_path):
    os.chdir(search_path)
    # A leading slash would make glob.glob treat the pattern as an
    # absolute filesystem path, searching outside the search root (e.g.
    # /*.py after chdir('/workspace') would match /top.py on
    # the host, not /workspace/top.py). Strip it so anchored globs
    # stay relative to the search root, matching the FilesystemBackend
    # semantics where slash anchors to the root, not the filesystem.
    rel_glob = glob_pat.lstrip('/')
    if any(seg == '..' for seg in rel_glob.replace(chr(92), '/').split('/')):
        sys.stderr.write('glob contains path traversal\\n')
        sys.exit(2)
    real_root = os.path.realpath(search_path)
    rel_files = sorted(glob.glob(rel_glob, recursive=True))
    # Open the glob-relative path (cwd is the search root) but report the
    # path prefixed with the search root, so GrepResult.path matches the
    # root/match form that grep -r emits on the --include route.
    targets = []
    for rel in rel_files:
        real_open = os.path.realpath(rel)
        if real_open != real_root and not real_open.startswith(real_root + os.sep):
            continue
        display_path = os.path.join(search_path, os.path.relpath(real_open, real_root))
        targets.append((real_open, display_path))
else:
    targets = [(search_path, search_path)]

for open_path, display_path in targets:
    try:
        with open(open_path, 'r', encoding='utf-8', errors='ignore') as fh:
            for i, line in enumerate(fh, 1):
                if pattern in line:
                    # GNU grep -HnFZ always terminates each record with a
                    # newline, even when the matched line has none. Strip
                    # the line's own trailing newline and add an explicit
                    # one so records never concatenate when a file's last
                    # line lacks a final newline.
                    sys.stdout.write(display_path + chr(0) + str(i) + ':' + line.rstrip(chr(10)) + chr(10))
                    match_count += 1
                    # Emit one record past the cap (match_count > max_count, not
                    # >=) so the parser can tell "exactly at the cap" (complete)
                    # from "capped early" (truncated). Mirrors the head -n
                    # max_count+1 route in _build_grep_cmd.
                    if max_count is not None and match_count > max_count:
                        sys.exit(0)
    except OSError:
        pass
" 2>/dev/null"""

_WRITE_CHECK_TEMPLATE = """python3 -c "
import os, base64

path = base64.b64decode('{path_b64}').decode('utf-8')
os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
" 2>&1"""

TRUNCATION_MSG: Final = (
    "\n\n[Output was truncated due to size limits. "
    "This paginated read result exceeded the sandbox stdout limit. "
    "Continue reading with a larger offset or smaller limit to inspect the rest of the file.]"
)

_EDIT_COMMAND_TEMPLATE = """python3 -c "
import sys, os, stat as _stat, base64, json

payload = json.loads(base64.b64decode(sys.stdin.read().strip()).decode('utf-8'))
path, old, new = payload['path'], payload['old'], payload['new']
replace_all = payload.get('replace_all', False)

try:
    st = os.stat(path)
    if not _stat.S_ISREG(st.st_mode):
        print(json.dumps({{'error': 'not_a_file'}}))
        sys.exit(0)

    with open(path, 'rb') as f:
        raw = f.read()

    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        print(json.dumps({{'error': 'not_a_text_file'}}))
        sys.exit(0)

    # Match-driven CRLF handling (issue #2880): the read template normalizes
    # CRLF to LF for the LLM, so old_string arrives LF-only even when the
    # file on disk is CRLF. Try old as sent, then a CRLF variant, then an LF
    # variant. The first match reveals the file line-ending style in that
    # region; apply the same transform to new so the file style is preserved.
    old_crlf = old.replace('\\r\\n', '\\n').replace('\\n', '\\r\\n')
    old_lf = old.replace('\\r\\n', '\\n')
    new_crlf = new.replace('\\r\\n', '\\n').replace('\\n', '\\r\\n')
    new_lf = new.replace('\\r\\n', '\\n')
    count = 0
    matched_old, matched_new = old, new
    for cand_old, cand_new in ((old, new), (old_crlf, new_crlf), (old_lf, new_lf)):
        c = text.count(cand_old)
        if c >= 1:
            matched_old, matched_new, count = cand_old, cand_new, c
            break

    if count == 0:
        print(json.dumps({{'error': 'string_not_found'}}))
        sys.exit(0)
    if count > 1 and not replace_all:
        print(json.dumps({{'error': 'multiple_occurrences', 'count': count}}))
        sys.exit(0)

    result = text.replace(matched_old, matched_new) if replace_all else text.replace(matched_old, matched_new, 1)
    with open(path, 'wb') as f:
        f.write(result.encode('utf-8'))

    print(json.dumps({{'count': count}}))
except FileNotFoundError:
    print(json.dumps({{'error': 'file_not_found'}}))
except PermissionError:
    print(json.dumps({{'error': 'permission_denied'}}))
" 2>&1 <<'__DEEPAGENTS_EDIT_EOF__'
{payload_b64}
__DEEPAGENTS_EDIT_EOF__
"""

_EDIT_TMPFILE_TEMPLATE = """python3 -c "
import os, stat as _stat, sys, json, base64

old_path = base64.b64decode('{old_path_b64}').decode('utf-8')
new_path = base64.b64decode('{new_path_b64}').decode('utf-8')
target = base64.b64decode('{target_b64}').decode('utf-8')
replace_all = {replace_all}

try:
    old = open(old_path, 'rb').read().decode('utf-8')
    new = open(new_path, 'rb').read().decode('utf-8')
except Exception as e:
    print(json.dumps({{'error': 'temp_read_failed', 'detail': str(e)}}))
    sys.exit(0)
finally:
    for p in (old_path, new_path):
        try: os.remove(p)
        except OSError: pass

try:
    st = os.stat(target)
    if not _stat.S_ISREG(st.st_mode):
        print(json.dumps({{'error': 'not_a_file'}}))
        sys.exit(0)

    with open(target, 'rb') as f:
        raw = f.read()

    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        print(json.dumps({{'error': 'not_a_text_file'}}))
        sys.exit(0)

    # Match-driven CRLF handling -- see _EDIT_COMMAND_TEMPLATE and issue #2880.
    old_crlf = old.replace('\\r\\n', '\\n').replace('\\n', '\\r\\n')
    old_lf = old.replace('\\r\\n', '\\n')
    new_crlf = new.replace('\\r\\n', '\\n').replace('\\n', '\\r\\n')
    new_lf = new.replace('\\r\\n', '\\n')
    count = 0
    matched_old, matched_new = old, new
    for cand_old, cand_new in ((old, new), (old_crlf, new_crlf), (old_lf, new_lf)):
        c = text.count(cand_old)
        if c >= 1:
            matched_old, matched_new, count = cand_old, cand_new, c
            break

    if count == 0:
        print(json.dumps({{'error': 'string_not_found'}}))
        sys.exit(0)
    if count > 1 and not replace_all:
        print(json.dumps({{'error': 'multiple_occurrences', 'count': count}}))
        sys.exit(0)

    result = text.replace(matched_old, matched_new) if replace_all else text.replace(matched_old, matched_new, 1)
    with open(target, 'wb') as f:
        f.write(result.encode('utf-8'))

    print(json.dumps({{'count': count}}))
except FileNotFoundError:
    print(json.dumps({{'error': 'file_not_found'}}))
except PermissionError:
    print(json.dumps({{'error': 'permission_denied'}}))
" 2>&1"""

_READ_COMMAND_TEMPLATE = """python3 -c "
import codecs, os, stat as _stat, sys, base64, json

MAX_OUTPUT_BYTES = 500 * 1024
MAX_BINARY_BYTES = 500 * 1024
MAX_LINE_COUNT_BYTES = 1024 * 1024
TRUNCATION_MSG = '\\n\\n' + (
    '[Output was truncated due to size limits. '
    'This paginated read result exceeded the sandbox stdout limit. '
    'Continue reading with a larger offset or smaller limit to inspect the rest of the file.]'
)

path = base64.b64decode('{path_b64}').decode('utf-8')

try:
    st = os.stat(path)
    if not _stat.S_ISREG(st.st_mode):
        print(json.dumps({{'error': 'not_a_file'}}))
        sys.exit(0)

    if st.st_size == 0:
        print(json.dumps({{'encoding': 'utf-8', 'content': 'System reminder: File exists but has empty contents'}}))
        sys.exit(0)

    file_type = '{file_type}'
    if file_type != 'text':
        if st.st_size > MAX_BINARY_BYTES:
            print(json.dumps({{'error': 'Binary file exceeds maximum preview size of ' + str(MAX_BINARY_BYTES) + ' bytes'}}))
            sys.exit(0)
        with open(path, 'rb') as f:
            raw = f.read()
        print(json.dumps({{'encoding': 'base64', 'content': base64.b64encode(raw).decode('ascii')}}))
        sys.exit(0)

    with open(path, 'rb') as f:
        raw_prefix = f.read(8192)

    # The 8192-byte prefix can slice a multi-byte UTF-8 char (CJK is 3 bytes,
    # emoji is 4); the incremental decoder buffers a trailing partial sequence
    # instead of raising, so legitimate text isn't misclassified as binary.
    is_binary = False
    try:
        codecs.getincrementaldecoder('utf-8')().decode(raw_prefix, final=False)
    except UnicodeDecodeError:
        is_binary = True

    if is_binary:
        with open(path, 'rb') as f:
            raw = f.read()
        print(json.dumps({{'encoding': 'base64', 'content': base64.b64encode(raw).decode('ascii')}}))
        sys.exit(0)

    offset = {offset}
    limit = {limit}

    # No lines requested: no line range to report. Reached whenever a caller
    # asks for zero lines, including a negative limit that _build_read_cmd
    # floored to 0; without this the empty window would fall through to the
    # offset-exceeds-length error below. Checked here, after the not-found,
    # directory, empty-file, and binary branches, so real failures and the
    # empty-file reminder are still reported first.
    if limit <= 0:
        print(json.dumps({{'encoding': 'utf-8', 'content': '', 'no_lines_requested': True}}))
        sys.exit(0)

    line_count = 0
    returned_lines = 0
    truncated = False
    parts = []
    current_bytes = 0
    msg_bytes = len(TRUNCATION_MSG.encode('utf-8'))
    effective_limit = MAX_OUTPUT_BYTES - msg_bytes

    at_eof = False
    with open(path, 'r', encoding='utf-8', newline=None) as f:
        while line_count < offset:
            raw_line = f.readline()
            if raw_line == '':
                at_eof = True
                break
            line_count += 1

        while not at_eof and returned_lines < limit and not truncated:
            raw_line = f.readline()
            if raw_line == '':
                at_eof = True
                break
            line_count += 1
            line = raw_line.rstrip('\\n').rstrip('\\r')
            piece = line if returned_lines == 0 else '\\n' + line
            piece_bytes = len(piece.encode('utf-8'))
            if current_bytes + piece_bytes > effective_limit:
                truncated = True
                remaining_bytes = effective_limit - current_bytes
                if remaining_bytes > 0:
                    prefix = piece.encode('utf-8')[:remaining_bytes].decode('utf-8', errors='ignore')
                    if prefix:
                        parts.append(prefix)
                        current_bytes += len(prefix.encode('utf-8'))
                break

            parts.append(piece)
            current_bytes += piece_bytes
            returned_lines += 1

        # The page can fill (returned_lines == limit) exactly at EOF without the
        # loop readline ever returning an empty string. Detect that via position:
        # after reading whole lines from a UTF-8 handle the decoder state is clean
        # at a line boundary, so tell() is the raw byte offset and equals st_size
        # at EOF. Worst case if this ever misjudges is a surfaced offset-exceeds-
        # length error on the next re-read (large files only, where total_lines
        # stays None) -- never a silent skip, since a false at_eof of True cannot
        # arise (a clean or packed tell() past EOF cannot equal st_size).
        if not at_eof:
            at_eof = f.tell() == st.st_size

    if returned_lines == 0 and not truncated:
        print(json.dumps({{'error': 'Line offset ' + str(offset) + ' exceeds file length (' + str(line_count) + ' lines)'}}))
        sys.exit(0)

    # When the page already reached EOF, reuse its scan's count for free.
    # Otherwise re-scan for the total only when the file is small enough that
    # the extra pass stays bounded; surrogateescape keeps an invalid byte after
    # the requested page from invalidating content that was decoded successfully.
    if at_eof:
        total_lines = line_count
    elif st.st_size <= MAX_LINE_COUNT_BYTES:
        with open(path, 'r', encoding='utf-8', errors='surrogateescape', newline=None) as f:
            total_lines = sum(1 for _ in f)
    else:
        total_lines = None

    text = ''.join(parts)
    if truncated:
        text += TRUNCATION_MSG

    # A byte cap can cut the final rendered line mid-way; that partial line is
    # deliberately not counted toward returned_lines (see the truncation
    # branch), so next_offset resumes at its start and the whole boundary line
    # is re-read. If even the first requested line overflows the cap no full
    # line was returned: advance by one so the read still makes progress instead
    # of looping on the same page (that line's tail is unreadable via line
    # offsets).
    if truncated and returned_lines == 0:
        returned_lines = 1

    end_line = offset + returned_lines
    if total_lines is not None:
        next_offset = end_line if end_line < total_lines else None
    else:
        # total_lines is None only via the large-file branch above, which is
        # reached only when the page stopped short of EOF, so lines always
        # remain here.
        next_offset = end_line
    print(json.dumps({{
        'encoding': 'utf-8',
        'content': text,
        'total_lines': total_lines,
        'start_line': offset + 1,
        'end_line': end_line,
        'next_offset': next_offset,
    }}))
except FileNotFoundError:
    print(json.dumps({{'error': 'file_not_found'}}))
except PermissionError:
    print(json.dumps({{'error': 'permission_denied'}}))
" 2>&1"""

_EXECUTE_CAPTURE_CMD_TEMPLATE = """# ===== deepagents capture-at-source offload (auto-generated wrapper) =====
# Runs the requested command below, capturing its combined output to a file in
# the sandbox: returned inline when small, or as a head/tail preview when large
# (the full result stays at the path for read_file). Disable this wrapping with
# BaseSandbox.enable_capture_offload = False.
__da_f=__PATH_Q__
__da_ecf="$__da_f.ec"
mkdir -p "$(dirname "$__da_f")" 2>/dev/null
# ----- requested command (verbatim, between the heredoc markers) -----
__da_cmd=$(cat <<'__DELIM__'
__COMMAND__
__DELIM__
)
# ----- end requested command; everything below is offload machinery -----
{ ( eval "$__da_cmd" ); echo "$?" > "$__da_ecf"; } 2>&1 | { head -c __MAXBYTES__ > "$__da_f"; cat > /dev/null; }
__da_ec=$(cat "$__da_ecf" 2>/dev/null)
: "${__da_ec:=1}"
rm -f "$__da_ecf"
__da_bytes=$(wc -c < "$__da_f" 2>/dev/null | tr -d ' ')
: "${__da_bytes:=0}"
__da_capped=0
[ "$__da_bytes" -ge __MAXBYTES__ ] && __da_capped=1
if [ "$__da_bytes" -le __BUDGET__ ]; then
  printf '%s %s %s %s\\n' '__SENTINEL__' "$__da_ec" 0 0
  cat "$__da_f"
  rm -f "$__da_f"
else
  __da_lines=$(wc -l < "$__da_f" 2>/dev/null | tr -d ' ')
  : "${__da_lines:=0}"
  __da_omitted=$((__da_lines - __HEADLINES__ - __TAILLINES__))
  printf '%s %s %s %s\\n' '__SENTINEL__' "$__da_ec" 1 "$__da_capped"
  if [ "$__da_omitted" -gt 0 ]; then
    head -c __HEAD__ "$__da_f" | head -n __HEADLINES__
    printf '... [%s lines truncated] ...\\n' "$__da_omitted"
    tail -c __TAIL__ "$__da_f" | tail -n __TAILLINES__
  else
    head -c $((__HEAD__ + __TAIL__)) "$__da_f"
  fi
fi
"""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _clip(text: str) -> str:
    if text:
        return text[:200]
    return "(empty)"


def _build_ls_cmd(path: str) -> str:
    """拼出列目录的远程脚本。运行时文本固定，源码排版不必与上游逐行相同。"""
    encoded = _b64(path)
    rows = [
        "import os",
        "import json",
        "import base64",
        "",
        "path = base64.b64decode('PATH_B64').decode('utf-8')",
        "",
        "try:",
        "    with os.scandir(path) as it:",
        "        for entry in it:",
        "            result = {",
        "                'path': os.path.join(path, entry.name),",
        "                'is_dir': entry.is_dir(follow_symlinks=False)",
        "            }",
        "            print(json.dumps(result))",
        "except FileNotFoundError:",
        "    print(json.dumps({'error': 'path_not_found'}))",
        "except NotADirectoryError:",
        "    print(json.dumps({'error': 'not_a_directory'}))",
        "except PermissionError:",
        "    print(json.dumps({'error': 'permission_denied'}))",
    ]
    script = "\n".join(rows).replace("PATH_B64", encoded)
    return 'python3 -c "' + "\n" + script + "\n" + '" 2>/dev/null'


def _parse_ls_output(output: str, path: str) -> LsResult:
    collected: list[dict[str, Any]] = []
    failure: str | None = None
    for row in output.strip().split("\n"):
        if row == "":
            continue
        try:
            payload = json.loads(row)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "error" in payload:
            failure = payload["error"]
            continue
        collected.append({"path": payload["path"], "is_dir": payload["is_dir"]})
    if failure is None:
        return LsResult(entries=collected)
    return LsResult(entries=None, error=f"Path '{path}': {failure}")


def _build_read_cmd(file_path: str, offset: int, limit: int) -> str:
    kind = _get_backend_read_file_type(file_path)
    start, count = normalize_read_bounds(offset, limit)
    return _READ_COMMAND_TEMPLATE.format(
        path_b64=_b64(file_path),
        file_type=kind,
        offset=start,
        limit=count,
    )


def _parse_read_output(output: str, file_path: str) -> ReadResult:
    output = output.rstrip()
    data: Any = None
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        data = None
    if not isinstance(data, dict):
        return ReadResult(error=f"File '{file_path}': unexpected server response: {_clip(output)}")
    if "error" in data:
        return ReadResult(error=f"File '{file_path}': {data['error']}")
    try:
        loaded = ReadResult(
            no_lines_requested=bool(data.get("no_lines_requested")),
            next_offset=data.get("next_offset"),
            end_line=data.get("end_line"),
            start_line=data.get("start_line"),
            total_lines=data.get("total_lines"),
            file_data=FileData(content=data["content"], encoding=data.get("encoding", "utf-8")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return ReadResult(error=f"File '{file_path}': unexpected server response: {exc}")
    return loaded


def _build_write_preflight_cmd(file_path: str) -> str:
    return _WRITE_CHECK_TEMPLATE.format(path_b64=_b64(file_path))


def _check_preflight_result(result: ExecuteResponse, file_path: str) -> WriteResult | None:
    blocked = result.exit_code != 0 or "Error:" in result.output
    if not blocked:
        return None
    error_msg = result.output.strip() or f"Failed to write file '{file_path}'"
    return WriteResult(error=error_msg)


def _build_grep_cmd(pattern: str, path: str | None, glob: str | None, max_count: int | None = None) -> str:
    root = path or "."
    if glob and "/" in glob:
        rendered = None if max_count is None else int(max_count)
        return _GREP_PATH_GLOB_TEMPLATE.format(
            path_b64=_b64(root),
            glob_b64=_b64(glob),
            pattern_b64=_b64(pattern),
            max_count=rendered,
        )
    include = ""
    if glob:
        include = "--include=" + shlex.quote(glob)
    command = f"grep -rHnFZ {include} -e {shlex.quote(pattern)} {shlex.quote(root)} 2>/dev/null"
    if max_count is None:
        return command + " || true"
    return command + f" | head -n {int(max_count) + 1} || true"


def _parse_grep_output(result: ExecuteResponse, path: str | None, max_count: int | None = None) -> GrepResult:
    output = result.output.rstrip("\n")
    if result.exit_code is not None and result.exit_code != 0:
        detail = output.strip() if output else f"exit code {result.exit_code}"
        return GrepResult(error=f"Path '{path or '.'}': {detail}")
    if output == "":
        return GrepResult(matches=[])
    found: list[dict[str, Any]] = []
    parse_error: str | None = None
    for line in output.split("\n"):
        try:
            file_path, rest = line.split("\0", 1)
            line_num_str, text = rest.split(":", 1)
            found.append({"path": file_path, "line": int(line_num_str), "text": text})
        except ValueError:
            parse_error = line
    if parse_error is not None and not found:
        return GrepResult(error=f"Path '{path or '.'}': {parse_error}")
    if max_count is not None and len(found) > max_count:
        return GrepResult(matches=found[:max_count], truncated=True)
    return GrepResult(matches=found)


def _build_glob_cmd(pattern: str, search_path: str) -> str:
    return _GLOB_COMMAND_TEMPLATE.format(path_b64=_b64(search_path), pattern_b64=_b64(pattern))


def _glob_search_root(path: str | None) -> str:
    if not path:
        return "/"
    if path.startswith("/"):
        return path
    return "/" + path


def _absolutize_glob_path(search_path: str, rel_path: str) -> str:
    if rel_path.startswith("/"):
        return rel_path
    return search_path.rstrip("/") + "/" + rel_path


def _classify_glob_line(line: str) -> tuple[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return "unparsed", line
    if not isinstance(payload, dict):
        return "unparsed", line
    if "error" in payload:
        return "error", payload["error"]
    if "warning" in payload:
        return "warning", payload
    if not isinstance(payload.get("path"), str):
        return "unparsed", line
    return "match", payload


def _glob_output_shortcut(result: ExecuteResponse, output: str, search_path: str) -> GlobResult | None:
    if result.exit_code is not None and result.exit_code != 0:
        detail = output[:200] if output else f"exit code {result.exit_code}"
        logger.error("Sandbox glob helper failed for path %r: %s", search_path, detail)
        return GlobResult(matches=None, error=f"Path '{search_path}': glob helper failed: {detail}")
    if output == "":
        reason = "transport" if result.truncated else None
        return GlobResult(matches=[], truncated=result.truncated, truncation_reason=reason)
    return None


def _glob_warning_reason(payload: dict[str, Any], current: str | None, search_path: str) -> str | None:
    kind = payload.get("warning")
    if kind != "walk_errors":
        return "budget" if current is None else current
    count = payload.get("count", "an unknown number of")
    sample = payload.get("sample", [])
    logger.warning("Sandbox glob could not read %s path(s) under %r; results are incomplete. Sample: %s", count, search_path, sample)
    return "unreadable"


def _parse_glob_output(result: ExecuteResponse, search_path: str) -> GlobResult:
    text = result.output.strip()
    shortcut = _glob_output_shortcut(result, text, search_path)
    if shortcut is not None:
        return shortcut
    matches: list[dict[str, Any]] = []
    unparsed: list[str] = []
    error: Any = None
    truncated = result.truncated
    reason = "transport" if result.truncated else None
    lines = text.split("\n")
    last_index = len(lines) - 1
    for index, line in enumerate(lines):
        if line.strip() == "":
            continue
        kind, payload = _classify_glob_line(line)
        if kind == "match":
            matches.append(
                {
                    "path": _absolutize_glob_path(search_path, payload["path"]),
                    "is_dir": bool(payload.get("is_dir", False)),
                }
            )
            continue
        if kind == "error":
            error = payload
            continue
        if kind == "warning":
            truncated = True
            reason = _glob_warning_reason(payload, reason, search_path)
            continue
        if result.truncated and index == last_index:
            logger.debug("Sandbox glob dropped a clipped final line for path %r: %s", search_path, payload[:200])
            continue
        unparsed.append(payload)
    if error is not None:
        logger.error("Sandbox glob returned error %r for path %r", error, search_path)
        return GlobResult(matches=None, error=f"Path '{search_path}': {error}")
    if unparsed:
        head = unparsed[0][:200]
        logger.error("Sandbox glob emitted %d unparseable line(s) for path %r; first: %s", len(unparsed), search_path, head)  # codespell:ignore unparseable
        return GlobResult(matches=None, error=f"Path '{search_path}': glob helper emitted unexpected output: {unparsed[0][:200]}")
    return GlobResult(matches=matches, truncated=truncated, truncation_reason=reason)


def _build_edit_inline_cmd(file_path: str, old_string: str, new_string: str, *, replace_all: bool) -> str:
    payload = json.dumps({"path": file_path, "old": old_string, "new": new_string, "replace_all": replace_all})
    return _EDIT_COMMAND_TEMPLATE.format(payload_b64=_b64(payload))


def _map_edit_error(error: str, file_path: str, old_string: str) -> EditResult:
    known = {
        "file_not_found": f"Error: File '{file_path}' not found",
        "permission_denied": f"Error: Permission denied editing file '{file_path}'",
        "not_a_file": f"Error: '{file_path}' is not a regular file",
        "not_a_text_file": f"Error: File '{file_path}' is not a text file",
        "string_not_found": f"Error: String not found in file: '{old_string}'",
        "multiple_occurrences": (
            f"Error: String '{old_string}' appears multiple times. Use replace_all=True to replace all occurrences."
        ),
    }
    message = known.get(error)
    if message is None:
        message = f"Error editing file '{file_path}': {error}"
    return EditResult(error=message)


def _parse_edit_output(output: str, file_path: str, old_string: str) -> EditResult:
    output = output.rstrip()
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        data = None
    if not isinstance(data, dict):
        return EditResult(error=f"Error editing file '{file_path}': unexpected server response: {_clip(output)}")
    if "error" in data:
        return _map_edit_error(data["error"], file_path, old_string)
    return EditResult(path=file_path, occurrences=data.get("count", 1))


def _build_edit_tmpfile_cmd(file_path: str, old_tmp: str, new_tmp: str, *, replace_all: bool) -> str:
    return _EDIT_TMPFILE_TEMPLATE.format(
        old_path_b64=_b64(old_tmp),
        new_path_b64=_b64(new_tmp),
        target_b64=_b64(file_path),
        replace_all=replace_all,
    )


def _edit_temp_paths() -> tuple[str, str]:
    uid = base64.b32encode(os.urandom(10)).decode("ascii").lower()
    stem = "/tmp/.deepagents_edit_" + uid
    return stem + "_old", stem + "_new"


def _new_heredoc_delim() -> str:
    token = base64.b32encode(os.urandom(10)).decode("ascii").rstrip("=")
    return "__DEEPAGENTS_CMD_" + token + "__"


def _build_capture_execute_cmd(
    command: str,
    capture_path: str,
    *,
    inline_budget: int,
    max_capture_bytes: int | None = None,
) -> str:
    cap = _EXECUTE_CAPTURE_MAX_BYTES if max_capture_bytes is None else max_capture_bytes
    delim = _new_heredoc_delim()
    while delim in command:
        delim = _new_heredoc_delim()
    steps = (
        ("__PATH_Q__", shlex.quote(capture_path)),
        ("__DELIM__", delim),
        ("__MAXBYTES__", str(cap)),
        ("__BUDGET__", str(inline_budget)),
        ("__SENTINEL__", _EXECUTE_CAPTURE_SENTINEL),
        ("__HEADLINES__", str(_EXECUTE_CAPTURE_HEAD_LINES)),
        ("__TAILLINES__", str(_EXECUTE_CAPTURE_TAIL_LINES)),
        ("__HEAD__", str(_EXECUTE_CAPTURE_HEAD_BYTES)),
        ("__TAIL__", str(_EXECUTE_CAPTURE_TAIL_BYTES)),
        ("__COMMAND__", command),
    )
    wrapped = _EXECUTE_CAPTURE_CMD_TEMPLATE
    for token, value in steps:
        wrapped = wrapped.replace(token, value)
    return wrapped


def _parse_capture_execute_output(output: str, *, backend_truncated: bool = False) -> ExecuteOffloadResult:
    head, _sep, body = output.partition("\n")
    fields = head.split(" ")
    exit_code: int | None = None
    usable = len(fields) == 4 and fields[0] == _EXECUTE_CAPTURE_SENTINEL
    if usable:
        try:
            exit_code = int(fields[1])
        except ValueError:
            usable = False
    if not usable or exit_code is None:
        return ExecuteOffloadResult(
            offloaded=False,
            response=ExecuteResponse(output=output, truncated=backend_truncated),
        )
    return ExecuteOffloadResult(
        offloaded=fields[2] == "1",
        response=ExecuteResponse(output=body, exit_code=exit_code, truncated=fields[3] == "1" or backend_truncated),
    )


def _edit_byte_size(old_string: str, new_string: str) -> int:
    return len(old_string.encode("utf-8")) + len(new_string.encode("utf-8"))


class BaseSandbox(SandboxBackendProtocol, abc.ABC):
    """沙箱文件操作的公共实现。子类提供执行、上传、下载和 ``id``。"""

    enable_capture_offload: bool = False

    @abc.abstractmethod
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """在沙箱里执行一条命令。"""
        ...

    def _snapshot_capture_target(
        self,
        command: str,
        capture_path: str,
        *,
        max_inline_bytes: int,
        max_capture_bytes: int | None,
    ) -> tuple[str, bool]:
        # 只在 execute 之前读一次。execute 里如果改掉这个属性，解析仍沿用这次的值。
        capture_offload = bool(self.enable_capture_offload)
        if not capture_offload:
            return command, False
        wrapped = _build_capture_execute_cmd(
            command,
            capture_path,
            inline_budget=max_inline_bytes,
            max_capture_bytes=max_capture_bytes,
        )
        return wrapped, True

    def _offload_from_snapshot(self, capture_offload: bool, result: ExecuteResponse) -> ExecuteOffloadResult:
        if not capture_offload:
            return ExecuteOffloadResult(offloaded=False, response=result)
        return _parse_capture_execute_output(result.output, backend_truncated=result.truncated)

    def execute_with_offload(self, command: str, capture_path: str, *, max_inline_bytes: int, max_capture_bytes: int | None = None, timeout: int | None = None) -> ExecuteOffloadResult:
        use_timeout = timeout is not None and execute_accepts_timeout(type(self))
        target, capture_offload = self._snapshot_capture_target(
            command,
            capture_path,
            max_inline_bytes=max_inline_bytes,
            max_capture_bytes=max_capture_bytes,
        )
        result = self.execute(target, timeout=timeout) if use_timeout else self.execute(target)
        return self._offload_from_snapshot(capture_offload, result)

    async def aexecute_with_offload(self, command: str, capture_path: str, *, max_inline_bytes: int, max_capture_bytes: int | None = None, timeout: int | None = None) -> ExecuteOffloadResult:
        use_timeout = timeout is not None and execute_accepts_timeout(type(self))
        target, capture_offload = self._snapshot_capture_target(
            command,
            capture_path,
            max_inline_bytes=max_inline_bytes,
            max_capture_bytes=max_capture_bytes,
        )
        if use_timeout:
            result = await self.aexecute(target, timeout=timeout)
        else:
            result = await self.aexecute(target)
        return self._offload_from_snapshot(capture_offload, result)

    def ls(self, path: str) -> LsResult:
        result = self.execute(_build_ls_cmd(path))
        return _parse_ls_output(result.output, path)

    async def als(self, path: str) -> LsResult:
        result = await self.aexecute(_build_ls_cmd(path))
        return _parse_ls_output(result.output, path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        result = self.execute(_build_read_cmd(file_path, offset, limit))
        return _parse_read_output(result.output, file_path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        response = await self.aexecute(_build_read_cmd(file_path, offset, limit))
        return _parse_read_output(response.output, file_path)

    def _write_preflight(self, file_path: str) -> WriteResult | None:
        result = self.execute(_build_write_preflight_cmd(file_path))
        return _check_preflight_result(result, file_path)

    async def _awrite_preflight(self, file_path: str) -> WriteResult | None:
        result = await self.aexecute(_build_write_preflight_cmd(file_path))
        return _check_preflight_result(result, file_path)

    def _accept_upload(self, file_path: str, responses: list[FileUploadResponse]) -> WriteResult:
        if responses:
            response = responses[0]
            if response.error:
                return WriteResult(error=f"Failed to write file '{file_path}': {response.error}")
            return WriteResult(path=file_path)
        msg = f"Responses was expected to return 1 result, but it returned {len(responses)} with type {type(responses)}"
        raise AssertionError(msg)

    def write(self, file_path: str, content: str) -> WriteResult:
        gate = self._write_preflight(file_path)
        if gate is not None:
            return gate
        payload = content.encode("utf-8")
        return self._accept_upload(file_path, self.upload_files([(file_path, payload)]))

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        gate = await self._awrite_preflight(file_path)
        if gate is not None:
            return gate
        payload = content.encode("utf-8")
        uploaded = await self.aupload_files([(file_path, payload)])
        return self._accept_upload(file_path, uploaded)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        if not old_string:
            return EditResult(error=EMPTY_OLD_STRING_ERROR)
        if _edit_byte_size(old_string, new_string) > _EDIT_INLINE_MAX_BYTES:
            return self._edit_via_upload(file_path, old_string, new_string, replace_all)
        return self._edit_inline(file_path, old_string, new_string, replace_all)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        if not old_string:
            return EditResult(error=EMPTY_OLD_STRING_ERROR)
        if _edit_byte_size(old_string, new_string) > _EDIT_INLINE_MAX_BYTES:
            return await self._aedit_via_upload(file_path, old_string, new_string, replace_all)
        return await self._aedit_inline(file_path, old_string, new_string, replace_all)

    def _edit_inline(self, file_path: str, old_string: str, new_string: str, replace_all: bool) -> EditResult:
        result = self.execute(_build_edit_inline_cmd(file_path, old_string, new_string, replace_all=replace_all))
        return _parse_edit_output(result.output, file_path, old_string)

    async def _aedit_inline(self, file_path: str, old_string: str, new_string: str, replace_all: bool) -> EditResult:
        result = await self.aexecute(
            _build_edit_inline_cmd(file_path, old_string, new_string, replace_all=replace_all)
        )
        return _parse_edit_output(result.output, file_path, old_string)

    def _edit_via_upload(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
    ) -> EditResult:
        old_tmp, new_tmp = _edit_temp_paths()
        resps = self.upload_files([(old_tmp, old_string.encode("utf-8")), (new_tmp, new_string.encode("utf-8"))])
        if len(resps) < 2:
            return EditResult(error=f"Error editing file '{file_path}': upload returned no response")
        for r in resps:
            if r.error:
                return EditResult(error=f"Error editing file '{file_path}': {r.error}")
        result = self.execute(_build_edit_tmpfile_cmd(file_path, old_tmp, new_tmp, replace_all=replace_all))
        return self._finish_uploaded_edit(result.output, file_path, old_string, old_tmp, new_tmp)

    async def _aedit_via_upload(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool,
    ) -> EditResult:
        old_tmp, new_tmp = _edit_temp_paths()
        resps = await self.aupload_files(
            [(old_tmp, old_string.encode("utf-8")), (new_tmp, new_string.encode("utf-8"))]
        )
        if len(resps) < 2:
            return EditResult(error=f"Error editing file '{file_path}': upload returned no response")
        for r in resps:
            if r.error:
                return EditResult(error=f"Error editing file '{file_path}': {r.error}")
        result = await self.aexecute(_build_edit_tmpfile_cmd(file_path, old_tmp, new_tmp, replace_all=replace_all))
        return await self._afinish_uploaded_edit(result.output, file_path, old_string, old_tmp, new_tmp)

    def _finish_uploaded_edit(
        self,
        raw: str,
        file_path: str,
        old_string: str,
        old_tmp: str,
        new_tmp: str,
    ) -> EditResult:
        output = raw.rstrip()
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            cleanup = self.execute(f"rm -f {shlex.quote(old_tmp)} {shlex.quote(new_tmp)}")
            if cleanup.exit_code != 0:
                logger.warning("Failed to clean up temp files for edit %s: %s", file_path, cleanup.output[:200])
            return EditResult(error=f"Error editing file '{file_path}': unexpected server response: {_clip(output)}")
        if not isinstance(data, dict):
            return EditResult(error=f"Error editing file '{file_path}': unexpected server response: {_clip(output)}")
        if "error" in data:
            code = data["error"]
            return _map_edit_error(code, file_path, old_string)
        times = data.get("count", 1)
        return EditResult(path=file_path, occurrences=times)

    async def _afinish_uploaded_edit(
        self,
        raw: str,
        file_path: str,
        old_string: str,
        old_tmp: str,
        new_tmp: str,
    ) -> EditResult:
        output = raw.rstrip()
        try:
            data = json.loads(output)
        except (json.JSONDecodeError, ValueError):
            cleanup = await self.aexecute(f"rm -f {shlex.quote(old_tmp)} {shlex.quote(new_tmp)}")
            if cleanup.exit_code != 0:
                logger.warning("Failed to clean up temp files for edit %s: %s", file_path, cleanup.output[:200])
            return EditResult(error=f"Error editing file '{file_path}': unexpected server response: {_clip(output)}")
        if not isinstance(data, dict):
            return EditResult(error=f"Error editing file '{file_path}': unexpected server response: {_clip(output)}")
        if "error" in data:
            code = data["error"]
            return _map_edit_error(code, file_path, old_string)
        times = data.get("count", 1)
        return EditResult(path=file_path, occurrences=times)

    def delete(self, file_path: str) -> DeleteResult:
        quoted = shlex.quote(file_path)
        probe = self.execute("test -e " + quoted + " || test -L " + quoted)
        absent = probe.exit_code is not None and probe.exit_code != 0
        if absent:
            return DeleteResult(error=f"Error: '{file_path}' not found")
        result = self.execute("rm -rf " + quoted)
        if result.exit_code == 0:
            return DeleteResult(path=file_path)
        return DeleteResult(error=f"Error deleting file '{file_path}': {result.output.strip() or 'unknown error'}")

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None) -> GrepResult:
        result = self.execute(_build_grep_cmd(pattern, path, glob, max_count))
        return _parse_grep_output(result, path, max_count)

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None) -> GrepResult:
        command = _build_grep_cmd(pattern, path, glob, max_count)
        try:
            result = await asyncio.wait_for(self.aexecute(command), timeout=ASYNC_GREP_TIMEOUT)
        except TimeoutError:
            logger.warning("agrep timed out after %ds (pattern=%r, path=%r, glob=%r)", ASYNC_GREP_TIMEOUT, pattern, path, glob)
            return GrepResult(error=f"Error: grep timed out after {ASYNC_GREP_TIMEOUT}s. Try a more specific pattern or a narrower path.")
        return _parse_grep_output(result, path, max_count)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        search_path = _glob_search_root(path)
        result = self.execute(_build_glob_cmd(pattern, search_path))
        return _parse_glob_output(result, search_path)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        search_path = _glob_search_root(path)
        command = _build_glob_cmd(pattern, search_path)
        try:
            result = await asyncio.wait_for(self.aexecute(command), timeout=ASYNC_GLOB_TIMEOUT)
        except TimeoutError:
            logger.warning("aglob timed out after %ds (pattern=%r, path=%r)", ASYNC_GLOB_TIMEOUT, pattern, search_path)
            return GlobResult(error=f"Error: glob timed out after {ASYNC_GLOB_TIMEOUT}s. Try a more specific pattern or a narrower path.")
        return _parse_glob_output(result, search_path)

    @property
    @abc.abstractmethod
    def id(self) -> str:
        """沙箱标识。"""
        ...

    @abc.abstractmethod
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """上传多个文件。单个失败写进对应响应。"""
        ...

    @abc.abstractmethod
    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """下载多个文件。单个失败写进对应响应。"""
        ...
