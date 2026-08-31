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
"""Unit tests for infer_state_and_unpack_ir and infer prompt assembly."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from dataagent.core.context.context import ContextFactory
from dataagent.utils.compression_utils import _prepare_prompt_to_infer_state_and_unpack, infer_state_and_unpack_ir


@pytest.fixture(autouse=True)
def _clear_context_factory():
    ContextFactory.clear_context()
    yield
    ContextFactory.clear_context()


@pytest.fixture(autouse=True)
def _stub_lineage_prompt_helpers(monkeypatch):
    monkeypatch.setattr(
        "dataagent.utils.converter.ir_message_consumer.build_available_actions",
        lambda *, runtime=None: "mock_tool: mock description\n",
    )


@pytest.fixture()
def context():
    ctx = ContextFactory.get_context(user_id="u", session_id="s", run_id=0, sub_id=0)
    ctx.register_query(query="test", additional_files=[])
    return ctx


def _mock_runtime(*, enable_profiling: bool = False, enable_summary: bool = False) -> MagicMock:
    runtime = MagicMock()
    runtime.get_config.side_effect = lambda key, default=False: {
        "CONTEXT.enable_profiling": enable_profiling,
        "CONTEXT.enable_summary": enable_summary,
    }.get(key, default)
    return runtime


def _patch_infer_llm(monkeypatch: pytest.MonkeyPatch, mock_llm: MagicMock) -> None:
    monkeypatch.setattr(
        "dataagent.utils.compression_utils.llm_manager.get_default_llm",
        lambda: mock_llm,
    )


class TestInferPromptAssembly:
    def test_prepare_prompt_state_only_when_profiling_disabled(self, context):
        prompts = _prepare_prompt_to_infer_state_and_unpack(context, runtime=None)
        assert len(prompts) == 2
        assert prompts[0]["role"] == "system"
        assert prompts[1]["role"] == "user"
        content = prompts[1]["content"]
        assert "<query>" in content
        assert "query: test" in content
        assert "<past_action>" in content
        system_content = prompts[0]["content"]
        assert "Provide ONE structured block" in system_content
        assert "Do NOT output `<unpack_data_ir>`" in system_content
        assert "--- Data IR Unpack List ---" not in system_content
        assert "<data_lineage>" not in content

    def test_prepare_prompt_includes_ir_sections_when_profiling_enabled(self, context):
        prompts = _prepare_prompt_to_infer_state_and_unpack(context, runtime=_mock_runtime(enable_profiling=True))
        assert "unpack_data_ir" in prompts[0]["content"]


class TestInferStateAndUnpackIr:
    @pytest.mark.asyncio
    async def test_parses_perfect_state_space_and_skips_unpack_when_profiling_disabled(self, context, monkeypatch):
        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(
                content=(
                    '<perfect_state_space>{"goal_intent": "g1", "belief_about_world": "b1", '
                    '"current_position": "p1"}</perfect_state_space>\n'
                    "<unpack_data_ir>[]</unpack_data_ir>"
                )
            )
        )
        _patch_infer_llm(monkeypatch, mock_llm)

        state_dict, unpacked = await infer_state_and_unpack_ir(context, runtime=_mock_runtime(enable_profiling=False))
        assert state_dict["goal_intent"] == "g1"
        assert state_dict["belief_about_world"] == "b1"
        assert state_dict["current_position"] == "p1"
        assert unpacked == ""

    @pytest.mark.asyncio
    async def test_extracts_ir_tokens_when_literal_eval_fails(self, context, monkeypatch, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("hello", encoding="utf-8")
        resolved = str(f.resolve())

        context.register_node(
            node_type="Action",
            label="a01",
            description="",
            predecessor_node=["Query(query00000)"],
            action="write_file",
            params={},
            output="ok",
            success=True,
        )
        file_label = context.register_node(
            node_type="File",
            label="f01",
            description="",
            predecessor_node=["Action(a01)"],
            edge_type="produces",
            path=resolved,
            source="write_file",
        )

        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(
                content=(
                    "<perfect_state_space>{}</perfect_state_space>\n"
                    f"<unpack_data_ir>not a list but mentions {file_label}</unpack_data_ir>"
                )
            )
        )
        _patch_infer_llm(monkeypatch, mock_llm)

        _, unpacked = await infer_state_and_unpack_ir(context, runtime=_mock_runtime(enable_profiling=True))
        assert file_label in unpacked
        assert "hello" in unpacked

    @pytest.mark.asyncio
    async def test_skips_recent_read_file_in_unpack(self, context, monkeypatch, tmp_path):
        f = tmp_path / "skip.txt"
        f.write_text("skip me", encoding="utf-8")
        resolved = str(f.resolve())

        context.register_node(
            node_type="State",
            description="",
            goal="",
            belief="",
            action_history="",
            current_status="",
            available_actions="",
            feedback="",
            uncentainty="",
            content="",
            reasoning_content="",
            predecessor_node=["Query(query00000)"],
        )
        context.register_node(
            node_type="Action",
            label="rf01",
            description="",
            predecessor_node=["State(state00000)"],
            action="read_file",
            params={"path": resolved},
            output="skip me",
            success=True,
            add_pt=True,
        )
        file_label = context.register_node(
            node_type="File",
            label="skip01",
            description="",
            predecessor_node=["Action(rf01)"],
            edge_type="produces",
            path=resolved,
            source="read_file",
        )

        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(
                content=(
                    "<perfect_state_space>{}</perfect_state_space>\n"
                    f"<unpack_data_ir>['{file_label}']</unpack_data_ir>"
                )
            )
        )
        _patch_infer_llm(monkeypatch, mock_llm)

        _, unpacked = await infer_state_and_unpack_ir(context, runtime=_mock_runtime(enable_profiling=True))
        assert unpacked == ""

    @pytest.mark.asyncio
    async def test_skips_ir_unpack_when_profiling_disabled(self, context, monkeypatch, tmp_path):
        f = tmp_path / "notes.txt"
        f.write_text("hello", encoding="utf-8")
        resolved = str(f.resolve())

        context.register_node(
            node_type="Action",
            label="a01",
            description="",
            predecessor_node=["Query(query00000)"],
            action="write_file",
            params={},
            output="ok",
            success=True,
        )
        file_label = context.register_node(
            node_type="File",
            label="f01",
            description="",
            predecessor_node=["Action(a01)"],
            edge_type="produces",
            path=resolved,
            source="write_file",
        )

        mock_llm = MagicMock()
        mock_llm.ainvoke = AsyncMock(
            return_value=MagicMock(
                content=(
                    '<perfect_state_space>{"goal_intent": "g1"}</perfect_state_space>\n'
                    f"<unpack_data_ir>['{file_label}']</unpack_data_ir>"
                )
            )
        )
        _patch_infer_llm(monkeypatch, mock_llm)

        state_dict, unpacked = await infer_state_and_unpack_ir(context, runtime=_mock_runtime(enable_profiling=False))
        assert state_dict["goal_intent"] == "g1"
        assert unpacked == ""


class TestDirectFoldFoldedMarker:
    """direct_fold must stamp ``_folded=True`` on the produced HumanMessage so
    that ``_compute_round_summaries`` can skip its (serialization-time) ``_ts``
    and avoid negative ``elapsed_sec``."""

    def test_folded_marker_present_on_content_response(self):
        from dataagent.utils.compression_utils import direct_fold

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="## SESSION INTENT\nrest...")
        result = direct_fold([], llm=mock_llm)
        assert len(result) == 1
        assert result[0].additional_kwargs.get("_folded") is True

    def test_folded_marker_present_on_reasoning_fallback(self):
        from dataagent.utils.compression_utils import direct_fold

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content=None, reasoning_content="## SESSION INTENT\nrest...")
        result = direct_fold([], llm=mock_llm)
        assert len(result) == 1
        assert result[0].additional_kwargs.get("_folded") is True

    def test_folded_marker_survives_serialize_deserialize(self):
        from dataagent.core.context.message_history import _deserialize, _serialize
        from dataagent.utils.compression_utils import direct_fold

        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(content="## SESSION INTENT\nrest...")
        result = direct_fold([], llm=mock_llm)
        ser = _serialize(result[0])
        assert ser["additional_kwargs"]["_folded"] is True
        deser = _deserialize(ser)
        assert deser.additional_kwargs.get("_folded") is True
