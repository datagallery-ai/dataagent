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
import inspect
from pathlib import Path

from dataagent.core.context import utils_context_filesystem as fs


def test_sha256_file_matches_hashlib_sha256(tmp_path: Path):
    """血缘文件摘要必须是 SHA-256，不能回退到 MD5。"""
    content = b"lineage-hash-lock"
    path = tmp_path / "sample.bin"
    path.write_bytes(content)

    digest = fs.sha256_file(p=str(path))

    assert digest == hashlib.sha256(content).hexdigest()
    assert len(digest) == 64
    assert digest != hashlib.md5(content).hexdigest()


def test_sha256_file_implementation_is_not_md5():
    """函数名和实现都必须是 sha256，避免字段/函数仍叫 md5。"""
    assert not hasattr(fs, "md5_file")
    source = inspect.getsource(fs.sha256_file)
    assert "sha256" in source
    assert "md5" not in source.lower()
