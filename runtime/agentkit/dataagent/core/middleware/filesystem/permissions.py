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
"""文件系统权限类型的公开入口。

类的定义不在本文件。这里只把 ``FilesystemPermission`` 从文件工具模块
取出来，并把它写成 ``import *`` 时的唯一名字。
"""

from dataagent.core.middleware.filesystem.filesystem import FilesystemPermission

__all__ = ["FilesystemPermission"]
