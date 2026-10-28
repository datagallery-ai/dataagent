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
"""de_agent suite HITL hooks 的运行期挂载通道测试（suite-root 直载 + 前缀解析）。

hooks.yaml 中注册的两个 HITL hook 走的是 suite-root 单文件直载通道
（与包路径 import 是两个模块身份）；本文件验证运行期真实通道可加载、
签名过 _validate_hook、合并前缀 data_engineering.* 可被 _try_resolve_suite_hook 解析。
"""

from __future__ import annotations

from dataagent.core.cbb.base_agent import BaseAgent
from dataagent.core.flex.agent import FlexAgent
from dataagent.utils.runtime_paths import dataagent_package_path

DE_AGENT_SUITE_ROOT = dataagent_package_path("core", "suite", "builtin_suites", "de_agent")
SUITE_NAME = "data_engineering"  # suite.yaml manifest name；目录名为 de_agent

HOOK_WIRING = (
    ("hooks.hitl_auto_resolver.hitl_auto_resolver", "nodes.human_feedback.pre"),
    ("hooks.text_question_guard.text_question_guard", "nodes.planner.post"),
    ("hooks.dataops_quality_check_post.dataops_quality_check_post", "agent.post"),
)


def test_hitl_hooks_load_via_suite_root_channel() -> None:
    """hooks.yaml 条目须能经 suite-root 单文件直载通道加载并通过签名校验。"""
    for rel_spec, location in HOOK_WIRING:
        fn = FlexAgent.import_hook_from_suite_root(
            rel_spec, root=DE_AGENT_SUITE_ROOT, suite_name=SUITE_NAME, location=location
        )
        BaseAgent._validate_hook(fn, location)


def test_hitl_hooks_resolve_via_merged_suite_prefix() -> None:
    """合并后的 ``data_engineering.hooks.*`` 条目须能经 _try_resolve_suite_hook 前缀解析。"""
    agent = object.__new__(FlexAgent)
    agent.config_manager = type(
        "_CM", (), {"activated_suites": [{"name": SUITE_NAME, "root": str(DE_AGENT_SUITE_ROOT)}]}
    )()
    for rel_spec, location in HOOK_WIRING:
        fn = agent._try_resolve_suite_hook(f"{SUITE_NAME}.{rel_spec}", location=location)
        assert fn is not None, rel_spec
        assert callable(fn)
