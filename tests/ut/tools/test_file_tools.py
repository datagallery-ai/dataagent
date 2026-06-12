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
"""Unit tests for file tool enhancements (phase 1).

Covers: read_file, edit_file, write_file, glob, grep.
"""

import sys
from pathlib import Path

import pytest

from dataagent.actions.tools.local_tool import tools
from dataagent.actions.tools.local_tool.sandbox import (
    NoopSandbox,
    reset_current_sandbox,
    set_current_sandbox,
)


@pytest.fixture()
def workspace(tmp_path: Path):
    """Set up a sandbox rooted at tmp_path and tear down after test."""
    root = tmp_path.resolve()
    token = set_current_sandbox(NoopSandbox(workspace_root=root))
    yield root
    reset_current_sandbox(token)


# ═══════════════════════════════════════════════════════════════════════════════
# read_file
# ═══════════════════════════════════════════════════════════════════════════════


class TestReadFileLineNumbers:
    def test_single_line(self, workspace: Path):
        (workspace / "a.txt").write_text("hello", encoding="utf-8")
        r = tools.read_file(str(workspace / "a.txt"), purpose="test")
        assert r["original_msg"] == "1\thello"

    def test_skill_alias_path(self, tmp_path: Path):
        workspace_root = (tmp_path / "workspace").resolve()
        skill_root = (tmp_path / "skills" / "pdf").resolve()
        workspace_root.mkdir(parents=True)
        skill_root.mkdir(parents=True)
        skill_file = skill_root / "SKILL.md"
        skill_file.write_text("---\nname: pdf\n---\n# PDF skill\n", encoding="utf-8")

        token = set_current_sandbox(NoopSandbox(workspace_root=workspace_root, skill_aliases={"pdf": skill_root}))
        try:
            r = tools.read_file("skill/pdf/SKILL.md", purpose="read skill entry")
        finally:
            reset_current_sandbox(token)

        assert "# PDF skill" in r["original_msg"]
        assert r["data"]["path"] == str(skill_file)

    def test_multi_line_with_offset(self, workspace: Path):
        (workspace / "b.txt").write_text("aaa\nbbb\nccc\nddd\n", encoding="utf-8")
        r = tools.read_file(str(workspace / "b.txt"), purpose="test", offset=2, limit=2)
        lines = r["original_msg"].split("\n")
        assert lines[0].startswith("2\t")
        assert lines[1].startswith("3\t")

    def test_cache_hit_also_has_line_numbers(self, workspace: Path):
        f = workspace / "c.txt"
        f.write_text("x\ny\n", encoding="utf-8")
        r1 = tools.read_file(str(f), purpose="test")
        r2 = tools.read_file(str(f), purpose="test")
        assert r1["original_msg"] == r2["original_msg"]
        assert r2["original_msg"].startswith("1\t")


class TestReadFileTokenBudget:
    def test_large_file_no_range_rejected(self, workspace: Path):
        f = workspace / "big.txt"
        f.write_bytes(b"x" * (257 * 1024))  # > 256KB
        with pytest.raises(ValueError, match="too large to read at once"):
            tools.read_file(str(f), purpose="test")

    def test_large_file_with_offset_allowed(self, workspace: Path):
        f = workspace / "big2.txt"
        f.write_bytes(b"line\n" * 60000)  # > 256KB
        r = tools.read_file(str(f), purpose="test", offset=1, limit=10)
        assert r["data"]["num_lines"] == 10

    def test_small_file_full_read_ok(self, workspace: Path):
        f = workspace / "small.txt"
        f.write_text("hello\nworld\n", encoding="utf-8")
        r = tools.read_file(str(f), purpose="test")
        assert "hello" in r["original_msg"]


class TestReadFileBOM:
    def test_utf8_bom(self, workspace: Path):
        f = workspace / "bom.txt"
        f.write_bytes(b"\xef\xbb\xbfhello BOM")
        r = tools.read_file(str(f), purpose="test")
        assert "hello BOM" in r["original_msg"]

    def test_binary_file_rejected(self, workspace: Path):
        f = workspace / "bin.dat"
        f.write_bytes(b"\x00\x01\x02\x03")
        with pytest.raises(ValueError, match="[Bb]inary"):
            tools.read_file(str(f), purpose="test")


# ═══════════════════════════════════════════════════════════════════════════════
# edit_file
# ═══════════════════════════════════════════════════════════════════════════════


class TestEditFileDiff:
    def test_replace_first_returns_diff(self, workspace: Path):
        f = workspace / "e.txt"
        f.write_text("aaa\nbbb\nccc\n", encoding="utf-8")
        r = tools.edit_file(str(f), op="replace_first", anchor="bbb", text="BBB", purpose="test")
        assert r["data"]["changed"] is True
        assert "-bbb" in r["original_msg"] or "bbb" in r["original_msg"]
        assert "+BBB" in r["original_msg"] or "BBB" in r["original_msg"]

    def test_no_change_returns_no_changes(self, workspace: Path):
        f = workspace / "f.txt"
        f.write_text("aaa\n", encoding="utf-8")
        r = tools.edit_file(str(f), op="replace_first", anchor="aaa", text="aaa", purpose="test")
        assert r["data"]["changed"] is False
        assert "no changes" in r["original_msg"].lower()


class TestEditFileEncoding:
    def test_preserves_crlf(self, workspace: Path):
        f = workspace / "crlf.txt"
        f.write_bytes(b"aaa\r\nbbb\r\nccc\r\n")
        tools.edit_file(str(f), op="replace_first", anchor="bbb", text="BBB", purpose="test")
        raw = f.read_bytes()
        assert b"\r\n" in raw
        assert b"BBB\r\n" in raw

    def test_preserves_lf(self, workspace: Path):
        f = workspace / "lf.txt"
        f.write_bytes(b"aaa\nbbb\nccc\n")  # 不要用 write_text
        tools.edit_file(str(f), op="replace_first", anchor="bbb", text="BBB", purpose="test")
        raw = f.read_bytes()
        assert b"\r\n" not in raw
        assert b"BBB\n" in raw

    def test_preserves_utf16le(self, workspace: Path):
        f = workspace / "u16.txt"
        content = "hello\nworld\n"
        f.write_bytes(b"\xff\xfe" + content.encode("utf-16le"))
        tools.edit_file(str(f), op="replace_first", anchor="hello", text="HELLO", purpose="test")
        raw = f.read_bytes()
        assert "HELLO".encode("utf-16le") in raw


class TestEditFileDeleteCleanup:
    def test_delete_full_line_no_extra_blank(self, workspace: Path):
        f = workspace / "del.txt"
        f.write_text("aaa\nbbb\nccc\n", encoding="utf-8")
        tools.edit_file(str(f), op="replace_first", anchor="bbb", text="", purpose="test")
        result = f.read_text(encoding="utf-8")
        # Should not have double newline where bbb was.
        assert "\n\n" not in result
        assert result == "aaa\nccc\n"


class TestEditFileLargeFile:
    def test_large_file_edit_succeeds(self, workspace: Path):
        f = workspace / "huge.txt"
        f.write_bytes(b"x" * (6 * 1024 * 1024))  # > 5MB
        tools.edit_file(str(f), op="replace_first", anchor="x", text="y", purpose="test")
        content = f.read_bytes()
        assert content[0:1] == b"y"


class TestEditFileAtomicWrite:
    @pytest.mark.skipif(sys.platform == "win32", reason="Windows does not preserve Unix permission bits")
    def test_preserves_permissions(self, workspace: Path):
        f = workspace / "perm.txt"
        f.write_text("aaa\n", encoding="utf-8")
        f.chmod(0o755)
        tools.edit_file(str(f), op="replace_first", anchor="aaa", text="bbb", purpose="test")
        mode = f.stat().st_mode
        assert mode & 0o755 == 0o755


# ═══════════════════════════════════════════════════════════════════════════════
# write_file
# ═══════════════════════════════════════════════════════════════════════════════


class TestWriteFileDiff:
    def test_create_no_diff(self, workspace: Path):
        f = workspace / "new.txt"
        r = tools.write_file(str(f), content="hello\n", purpose="test")
        assert r["data"]["type"] == "create"
        # No diff for new files — just confirmation.
        assert "---" not in r["original_msg"]

    def test_update_has_diff(self, workspace: Path):
        f = workspace / "upd.txt"
        f.write_text("old content\n", encoding="utf-8")
        r = tools.write_file(str(f), content="new content\n", purpose="test")
        assert r["data"]["type"] == "update"
        assert "-old content" in r["original_msg"]
        assert "+new content" in r["original_msg"]


class TestWriteFileLargeContent:
    def test_large_content_succeeds(self, workspace: Path):
        large = "x" * (6 * 1024 * 1024)
        tools.write_file(str(workspace / "x.txt"), content=large, purpose="test")
        assert (workspace / "x.txt").read_text(encoding="utf-8") == large


class TestWriteFileAtomicWrite:
    @pytest.mark.skipif(sys.platform == "win32", reason="Windows does not preserve Unix permission bits")
    def test_preserves_permissions_on_update(self, workspace: Path):
        f = workspace / "perm2.txt"
        f.write_text("old\n", encoding="utf-8")
        f.chmod(0o755)
        tools.write_file(str(f), content="new\n", purpose="test")
        mode = f.stat().st_mode
        assert mode & 0o755 == 0o755

    def test_creates_parent_dirs(self, workspace: Path):
        f = workspace / "sub" / "dir" / "file.txt"
        tools.write_file(str(f), content="hello\n", purpose="test")
        assert f.read_text(encoding="utf-8") == "hello\n"


# ═══════════════════════════════════════════════════════════════════════════════
# glob
# ═══════════════════════════════════════════════════════════════════════════════


class TestGlob:
    def test_basic_match(self, workspace: Path):
        (workspace / "a.py").write_text("", encoding="utf-8")
        (workspace / "b.txt").write_text("", encoding="utf-8")
        r = tools.glob("*.py", target_directory=str(workspace))
        assert "a.py" in r["data"]["paths"]
        assert "b.txt" not in r["data"]["paths"]

    def test_returns_relative_paths(self, workspace: Path):
        (workspace / "hello.py").write_text("", encoding="utf-8")
        r = tools.glob("*.py", target_directory=str(workspace))
        for p in r["data"]["paths"]:
            assert not p.startswith("/"), f"Expected relative path, got: {p}"

    def test_excludes_git_directory(self, workspace: Path):
        git_dir = workspace / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("", encoding="utf-8")
        (workspace / "real.txt").write_text("", encoding="utf-8")
        r = tools.glob("*", target_directory=str(workspace))
        paths = r["data"]["paths"]
        assert any("real.txt" in p for p in paths)
        assert not any(".git" in p for p in paths)

    def test_truncation_message(self, workspace: Path):
        for i in range(5):
            (workspace / f"f{i}.txt").write_text("", encoding="utf-8")
        r = tools.glob("*.txt", target_directory=str(workspace), max_results=3)
        assert r["data"]["truncated"] is True
        assert "truncated" in r["original_msg"].lower()

    def test_auto_prepend_double_star(self, workspace: Path):
        sub = workspace / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("", encoding="utf-8")
        r = tools.glob("*.py", target_directory=str(workspace))
        assert any("deep.py" in p for p in r["data"]["paths"])


# ═══════════════════════════════════════════════════════════════════════════════
# grep
# ═══════════════════════════════════════════════════════════════════════════════


class TestGrep:
    def _make_files(self, workspace: Path):
        (workspace / "a.py").write_text("def hello():\n    pass\n", encoding="utf-8")
        (workspace / "b.txt").write_text("hello world\ngoodbye\n", encoding="utf-8")

    def test_file_type_filter(self, workspace: Path):
        self._make_files(workspace)
        r = tools.grep("hello", path=str(workspace), file_type="py")
        assert "a.py" in r["original_msg"]
        assert "b.txt" not in r["original_msg"]

    def test_files_with_matches_default(self, workspace: Path):
        self._make_files(workspace)
        r = tools.grep("hello", path=str(workspace))
        # Default mode is files_with_matches.
        assert "a.py" in r["original_msg"]
        assert "b.txt" in r["original_msg"]

    def test_content_mode(self, workspace: Path):
        self._make_files(workspace)
        r = tools.grep("hello", path=str(workspace), output_mode="content")
        # Content mode includes line numbers.
        assert "1:" in r["original_msg"] or "1-" in r["original_msg"]

    def test_count_mode(self, workspace: Path):
        self._make_files(workspace)
        r = tools.grep("hello", path=str(workspace), output_mode="count")
        assert "1" in r["original_msg"]

    def test_case_insensitive(self, workspace: Path):
        (workspace / "ci.txt").write_text("Hello World\n", encoding="utf-8")
        r = tools.grep("hello", path=str(workspace), case_insensitive=True)
        assert "ci.txt" in r["original_msg"]

    def test_case_sensitive_default(self, workspace: Path):
        (workspace / "cs.txt").write_text("Hello World\n", encoding="utf-8")
        r = tools.grep("hello", path=str(workspace), case_insensitive=False)
        assert "cs.txt" not in r["original_msg"]

    def test_context_lines(self, workspace: Path):
        (workspace / "ctx.txt").write_text("aaa\nbbb\nccc\nddd\neee\n", encoding="utf-8")
        r = tools.grep("ccc", path=str(workspace), output_mode="content", context=1)
        assert "bbb" in r["original_msg"]
        assert "ddd" in r["original_msg"]

    def test_excludes_git_directory(self, workspace: Path):
        git_dir = workspace / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("secret_token\n", encoding="utf-8")
        (workspace / "real.txt").write_text("secret_token\n", encoding="utf-8")
        r = tools.grep("secret_token", path=str(workspace))
        assert "real.txt" in r["original_msg"]
        assert ".git" not in r["original_msg"]

    def test_relative_paths_in_output(self, workspace: Path):
        (workspace / "rel.txt").write_text("findme\n", encoding="utf-8")
        r = tools.grep("findme", path=str(workspace))
        assert not r["original_msg"].startswith("/")

    def test_truncation_message(self, workspace: Path):
        lines = "\n".join(f"match_{i}" for i in range(300))
        (workspace / "many.txt").write_text(lines, encoding="utf-8")
        r = tools.grep("match_", path=str(workspace), output_mode="content", head_limit=10)
        assert r["data"]["truncated"] is True
        assert "truncated" in r["original_msg"].lower()

    def test_no_matches(self, workspace: Path):
        (workspace / "empty.txt").write_text("nothing here\n", encoding="utf-8")
        r = tools.grep("zzzzz_nonexistent", path=str(workspace))
        assert "no matches" in r["original_msg"].lower()
