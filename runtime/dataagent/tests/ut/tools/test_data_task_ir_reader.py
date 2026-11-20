# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""DataTaskIR 口径读取器（只读）—— 主键重复率 / 抽样回源比对的输入来源。

**根因**（生产实测三次运行 ``self_correct.status`` 分别是 ``not_converged``
/ ``not_converged`` / ``not_needed``，而 ``pk_duplicate_rate`` 与
``source_reconciliation`` 始终是 ``skipped``）：

``ir_field_values`` 是 ``Runtime`` 的**进程内内存缓存**，只由 IR 的
post-hook（``create_ir`` / ``update_ir``）写入。「第二轮输入『执行数据
校验』」走的是另一个工具，不触发那两个 hook → 缓存为空 → 主键拿不到。

而 TASKIR 的权威口径持久化在 IR 会话快照里（*Runtime caches are never the
source of truth*）。本模块让校验链路能**读到**那份已冻结的口径。
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from dataagent.actions.tools.hooks.examples.data_task_ir_spike.reader import (
    load_committed_fields,
)
from dataagent.actions.tools.hooks.examples.data_task_ir_spike.session_store import (
    IRSessionStore,
)
from dataagent.actions.tools.local_tool import dataops_quality_check as qc


class _Runtime:
    """最小 runtime 替身 —— 只提供本模块用到的东西。"""

    def __init__(
        self,
        workspace=None,
        user_id="u",
        session_id="s",
        config=None,
        sub_id=0,
        hierarchy="MAIN",
    ):
        self.workspace_dir = Path(workspace) if workspace else None
        self.user_id = user_id
        self.session_id = session_id
        self.sub_id = sub_id
        self.hierarchy = hierarchy
        self._cache: dict[str, object] = {}
        self._config = config or {}

    def get_cache(self, key: str, default: object = None) -> object:
        return self._cache.get(key, default)

    def set_cache(self, key: str, value: object) -> None:
        self._cache[key] = value

    def get_all_config(self) -> dict:
        return dict(self._config)


IDENTITY_FIELDS = [
    {
        "field_id": "identity",
        "value": {
            "components": [
                {"field_ref": "app_type"},
                {"field_ref": "conversion_target"},
            ]
        },
    }
]


def _store(tmp: Path, user_id="u", session_id="s") -> IRSessionStore:
    """按 ``prepare_turn`` 的同一套路径解析定位 IR 快照存储。

    ``resolve_flex_session_memory_dir`` 在没有 ``WORKSPACE.path`` 配置时落到
    ``{workspace}/<layout.session_memory_dir>``，所以这里直接用
    ``workspace/.memory`` 构造 store —— 与实际落盘位置一致。
    """
    memory = tmp / ".memory"
    return IRSessionStore(memory, user_id, session_id)


def _snapshot_payload(
    fields,
    *,
    status="committed",
    updated_at="2026-11-19T10:00:00+00:00",
    owner=None,
):
    """构造一份合法的快照（字段形状与 ``IRSessionStore.save`` 落盘的一致）。"""
    return {
        "schema_version": 1,
        "owner": owner or {"user_id": "u", "session_id": "s"},
        "status": status,
        # 文件名安全的 revision（Windows 不允许 ``:``）—— 与真实落盘的
        # ``uuid4().hex`` 同样是十六进制串。
        "revision": "rev" + updated_at.replace("-", "").replace(":", "").replace("+", ""),
        "parent_revision": None,
        "task_id": "task-1",
        "queries": ["生成转化率汇总表"],
        "fields": fields,
        "evidence": [],
        "fingerprint": "fp",
        "turn_id": "turn-1",
        "relation": "new",
        "reason": "首次数据任务",
        "updated_at": updated_at,
    }


def _write_snapshot(
    store: IRSessionStore,
    fields,
    *,
    status="committed",
    updated_at="2026-11-19T10:00:00+00:00",
):
    """把一份快照写成 ``current.json`` **和** 一个不可变版本文件。

    **不直接用 ``IRSessionStore.save``** —— 它的文件锁用 ``fcntl``（POSIX
    专有），在 Windows 上直接 ``ModuleNotFoundError``。这里手工落盘出
    与 ``save`` 完全相同的两种文件，验证读取器对两者的处理。
    """
    payload = _snapshot_payload(fields, status=status, updated_at=updated_at)
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / "versions").mkdir(parents=True, exist_ok=True)
    (store.root / "versions" / f"{payload['revision']}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    (store.root / "current.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return payload


# ============================================================================
# 1) 缓存优先
# ============================================================================
class TestCacheFirst:
    def test_cache_is_used_when_present(self):
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = IDENTITY_FIELDS
        assert load_committed_fields(runtime) == IDENTITY_FIELDS

    def test_cache_wins_over_snapshot(self):
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = IDENTITY_FIELDS
        runtime._cache["ir_session"] = {
            "snapshot": {"status": "committed", "fields": [{"field_id": "identity", "value": {}}]}
        }
        assert load_committed_fields(runtime) == IDENTITY_FIELDS


# ============================================================================
# 2) 本轮快照
# ============================================================================
class TestSessionSnapshot:
    def test_committed_snapshot_is_used(self):
        runtime = _Runtime()
        runtime._cache["ir_session"] = {
            "snapshot": {"status": "committed", "fields": IDENTITY_FIELDS}
        }
        assert load_committed_fields(runtime) == IDENTITY_FIELDS

    def test_draft_snapshot_is_not_authoritative(self):
        runtime = _Runtime()
        runtime._cache["ir_session"] = {
            "snapshot": {"status": "draft", "fields": IDENTITY_FIELDS}
        }
        assert load_committed_fields(runtime) == []

    def test_awaiting_relation_snapshot_is_not_authoritative(self):
        runtime = _Runtime()
        runtime._cache["ir_session"] = {
            "snapshot": {"status": "awaiting_relation", "fields": IDENTITY_FIELDS}
        }
        assert load_committed_fields(runtime) == []


# ============================================================================
# 3) 磁盘回落 —— 本轮 runtime 完全没有 IR 缓存时的真实场景
# ============================================================================
class TestDiskFallback:
    def test_disk_snapshot_is_read_when_runtime_has_no_cache(self):
        """**核心回归**：第二轮的 runtime 什么缓存都没有，主键必须拿得到。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            _write_snapshot(store, IDENTITY_FIELDS)

            runtime = _Runtime(workspace=root)
            # 真实第二轮：缓存全空
            assert runtime.get_cache("ir_field_values") is None
            assert runtime.get_cache("ir_session") is None

            fields = load_committed_fields(runtime)
            assert fields == IDENTITY_FIELDS
            # 并且能真的解析出主键
            assert qc.resolve_primary_key_columns(runtime, ["app_type", "conversion_target"]) == (
                "app_type",
                "conversion_target",
            )

    def test_draft_current_pointer_falls_back_to_latest_committed_version(self):
        """``current.json`` 被改写成 draft 时，仍能取回上一份已冻结口径。

        ``select_task`` 在任务关系变更时会把 ``current.json`` 写成 draft
        （``fields`` 可能被清空）—— 那时靠 ``current.json`` 已经拿不到
        口径，但 ``versions/`` 里的历史版本是不可变的。
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            _write_snapshot(store, IDENTITY_FIELDS, updated_at="2026-11-19T10:00:00+00:00")
            # 再写一份 draft（current.json 被改写）
            _write_snapshot(
                store,
                [],
                status="draft",
                updated_at="2026-11-19T12:00:00+00:00",
            )

            runtime = _Runtime(workspace=root)
            fields = load_committed_fields(runtime)
            assert fields == IDENTITY_FIELDS

    def test_no_snapshot_anywhere_yields_empty(self):
        with TemporaryDirectory() as tmp:
            runtime = _Runtime(workspace=Path(tmp))
            assert load_committed_fields(runtime) == []

    def test_runtime_without_workspace_yields_empty(self):
        runtime = _Runtime(workspace=None)
        assert load_committed_fields(runtime) == []

    def test_corrupt_current_json_does_not_raise(self):
        """快照损坏不能让整轮校验崩掉 —— 退回 ``versions/`` 或返回空。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            _write_snapshot(store, IDENTITY_FIELDS)
            (store.root / "current.json").write_text("{not json", encoding="utf-8")

            runtime = _Runtime(workspace=root)
            fields = load_committed_fields(runtime)
            # 损坏的 current.json → 回落到 versions/ 里的 committed 版本
            assert fields == IDENTITY_FIELDS

    def test_reader_never_writes_to_disk(self):
        """只读契约：读完之后磁盘内容不能变。"""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            _write_snapshot(store, IDENTITY_FIELDS)
            before = {
                path: path.read_bytes()
                for path in sorted(store.root.rglob("*.json"))
            }
            load_committed_fields(_Runtime(workspace=root))
            after = {
                path: path.read_bytes()
                for path in sorted(store.root.rglob("*.json"))
            }
            assert before == after


# ============================================================================
# 3b) 子 Agent 不得继承主 Agent 的会话级口径
# ============================================================================
class TestSubAgentIsolation:
    """IR 快照是**会话级**的（按 user/session 定位）。

    子 Agent 与主 Agent 共享同一份存储，而 ``ensure_task_bound`` 的语义是
    「子 Agent 不继承主 Agent 的历史口径」（``prepare_turn`` 里
    ``sub_id != 0`` 直接返回）。所以磁盘回落**只对 MAIN 主 Agent 生效** ——
    否则子 Agent 会把**另一个任务**的口径当成自己的主键声明。
    """

    def test_sub_agent_does_not_read_main_snapshot(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            _write_snapshot(store, IDENTITY_FIELDS)

            sub = _Runtime(workspace=root, sub_id=1)
            assert load_committed_fields(sub) == []

    def test_sub_hierarchy_does_not_read_main_snapshot(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            _write_snapshot(store, IDENTITY_FIELDS)

            sub = _Runtime(workspace=root, hierarchy="SUB")
            assert load_committed_fields(sub) == []

    def test_main_agent_still_reads(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            _write_snapshot(store, IDENTITY_FIELDS)

            main = _Runtime(workspace=root, sub_id=0, hierarchy="MAIN")
            assert load_committed_fields(main) == IDENTITY_FIELDS


# ============================================================================
# 4) 端到端：主键从磁盘口径解析出来
# ============================================================================
class TestEndToEndPrimaryKey:
    def test_primary_key_resolved_from_disk_snapshot(self):
        """**这是用户报的那个 bug 的回归防护**。

        主键重复率与抽样回源比对都靠 ``ctx.primary_key_columns``；拿不到
        主键 → 两项整项 ``skipped`` → 自修正没有触发依据。
        """
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            _write_snapshot(store, IDENTITY_FIELDS)

            runtime = _Runtime(workspace=root)
            assert qc._primary_key_from_ir(runtime) == ("app_type", "conversion_target")
            assert qc.resolve_primary_key_columns(
                runtime, ["app_type", "conversion_target", "total_conversion_cnt"]
            ) == ("app_type", "conversion_target")

    def test_grain_resolved_from_disk_snapshot(self):
        """聚合表的主键记在 ``output_features[].aggregation.grain`` 里。"""
        grain_fields = [
            {
                "field_id": "output_features",
                "value": {
                    "features": [
                        {"aggregation": {"grain": ["app_type", "conversion_target"]}},
                    ]
                },
            }
        ]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = _store(root)
            _write_snapshot(store, grain_fields)

            runtime = _Runtime(workspace=root)
            assert qc._primary_key_from_ir_grain(runtime) == ("app_type", "conversion_target")


# ============================================================================
# 4b) 跳过原因必须指向真正的责任方
# ============================================================================
class TestSkipReasonDiagnostics:
    """跳过原因不能一律写「需要人工补主键配置」。

    生产实测那次**不是**配置缺失：IR 里已经有主键声明，只是链路没读到。
    文案把责任推给配置，会让人以为「补个配置就好了」。
    """

    def _reason(self, runtime, fields):
        ctx = qc.build_check_context(
            temp_table="adhoctemp.tmp_x",
            fields=fields,
            partition_col="pt_d",
            partition_date="20260101",
            sample_size=100,
            checkers=(),
        )
        return qc._skip_reason(
            qc.PK_DUPLICATE_CHECKER,
            (),
            fields,
            ctx,
            runtime=runtime,
        )

    def test_declared_but_unmatched_points_to_the_chain_not_the_config(self):
        runtime = _Runtime()
        runtime._cache["ir_field_values"] = IDENTITY_FIELDS
        reason = self._reason(runtime, ["app_type"])  # 目标表只有一列 → 整组作废
        assert "DataTaskIR 已声明主键" in reason
        assert "app_type" in reason
        assert "未找到主键声明" not in reason

    def test_genuinely_missing_declaration_is_reported_as_missing(self):
        runtime = _Runtime()
        reason = self._reason(runtime, ["app_type"])
        assert "未找到主键声明" in reason


# ============================================================================
# 5) 不给校验工具挂 ensure_ir_ready hook
# ============================================================================
class TestNoIrRefreshHookOnValidationTools:
    """校验工具**不挂** ``ensure_ir_ready``。

    那个 hook 会触发 IR 刷新的 LLM 调用，且 HITL 队列未清时直接 ``raise``
    —— 会把「执行数据校验」打断成一次失败的任务。校验链路要的是**读**
    已冻结的口径，不是**刷新**口径。

    这里从**合并后的工具配置**里核对，而不是核对 YAML 源文件 ——
    生效的配置才是真正跑的。
    """

    def _effective_hooks(self) -> dict[str, list[str]]:
        import yaml

        from dataagent.utils.runtime_paths import dataagent_package_path

        path = dataagent_package_path(
            "core", "suite", "builtin_suites", "de_agent", "tools", "tools.yaml"
        )
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        hooks: dict[str, list[str]] = {}
        for section in ("builtin", "local_functions"):
            for entry in (config.get("TOOLS") or {}).get(section) or []:
                name = entry.get("name")
                if not name:
                    continue
                hooks[name] = [
                    str(item)
                    for phase in (entry.get("hooks") or {}).values()
                    for item in (phase or [])
                ]
        return hooks

    def test_validation_tools_have_no_ir_refresh_pre_hook(self):
        hooks = self._effective_hooks()
        for tool_name in ("dataops_validate_data", "trial_compute_table"):
            assert tool_name in hooks, f"{tool_name} 未在 tools.yaml 里注册"
            pre_hooks = [
                item
                for item in hooks[tool_name]
                if "ensure_ir_ready" in item or "update_ir" in item
            ]
            assert not pre_hooks, f"{tool_name} 不该挂 IR 刷新 hook：{pre_hooks}"
