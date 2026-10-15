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
"""de_agent suite hooks 包（提供包路径 import 身份，便于测试与本地调试）。

运行期 hook 经 suite-root 单文件直载加载（见 hooks.yaml 头注释），本包
不参与 hook 解析。hook 模块具双重模块身份，禁止模块级 mutable 状态。
"""
