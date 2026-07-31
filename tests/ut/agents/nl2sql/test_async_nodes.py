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
import asyncio
import inspect
import threading
from unittest.mock import patch

import pytest

from dataagent.agents.nl2sql.nodes.base_nl2sql_node import BaseNL2SQLNode
from dataagent.agents.nl2sql.nodes.executor import ExecutorNode
from dataagent.agents.nl2sql.nodes.generator import GeneratorNode
from dataagent.agents.nl2sql.nodes.perceptor import PerceptorNode
from dataagent.agents.nl2sql.nodes.reflector import ReflectorNode
from dataagent.agents.nl2sql.nodes.selector import SelectorNode
from dataagent.agents.nl2sql.nodes.udn_perceptor import UDNPerceptorNode
from dataagent.agents.nl2sql.nodes.validator import ValidatorNode
from dataagent.agents.nl2sql.workflow.state import Result, get_default_state


class _ConfigManager:
    def get(self, _key, default=None):
        """Return the supplied default configuration value."""
        return default


class _AsyncGeneratorNode(GeneratorNode):
    async def run_strategy(self, strategy, settings, context):
        """Return deterministic strategy results after different delays."""
        await asyncio.sleep(0.02 if strategy == "slow" else 0.001)
        return [(f"SELECT '{strategy}'", f"prompt-{strategy}", strategy)]


class _FailingGeneratorNode(GeneratorNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.slow_started = asyncio.Event()
        self.slow_done = asyncio.Event()
        self.slow_cancelled = False

    async def run_strategy(self, strategy, settings, context):
        """Fail one strategy after its sibling has started."""
        if strategy == "fail":
            await self.slow_started.wait()
            raise RuntimeError("strategy failed")
        self.slow_started.set()
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            self.slow_cancelled = True
            raise
        finally:
            self.slow_done.set()
        return [("SELECT 1", "prompt-slow", strategy)]


class _AllFailingGeneratorNode(GeneratorNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0

    async def run_strategy(self, strategy, settings, context):
        """Fail every strategy after recording that it ran."""
        self.calls += 1
        await asyncio.sleep(0.001 if strategy == "fast-fail" else 0.01)
        raise RuntimeError(f"{strategy} failed")


class _BlockingGeneratorNode(GeneratorNode):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.all_started = asyncio.Event()
        self.started = 0
        self.cancelled = 0
        self.expected_tasks = self.num_workers * len(self.strategies)

    async def run_strategy(self, strategy, settings, context):
        """Wait indefinitely so outer Generator cancellation can be observed."""
        self.started += 1
        if self.started == self.expected_tasks:
            self.all_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class _ThreadTrackingService:
    def __init__(self):
        self.thread_ids = []

    def __enter__(self):
        self.thread_ids.append(threading.get_ident())
        return self

    def __exit__(self, *_args):
        self.thread_ids.append(threading.get_ident())
        return False

    def execute(self, _sql):
        """Return a deterministic row while recording the worker thread."""
        self.thread_ids.append(threading.get_ident())
        return ["value"], [(1,)], None

    def explain(self, _sql):
        """Return a successful EXPLAIN while recording the worker thread."""
        self.thread_ids.append(threading.get_ident())
        return None


class _BlockingService:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.closed_event = threading.Event()
        self.closed = False
        self.closed_during_execute = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        self.closed_event.set()
        return False

    def execute(self, _sql):
        """Block until released and record whether cleanup raced the query."""
        self.started.set()
        self.release.wait(timeout=1)
        self.closed_during_execute = self.closed
        self.finished.set()
        return ["value"], [(1,)], None


@pytest.mark.asyncio
async def test_generator_preserves_completion_order() -> None:
    """Generator should preserve completion-ordered result collection."""
    node = _AsyncGeneratorNode(
        config_manager=_ConfigManager(), strategies=["slow", "fast"], num_workers=1, num_samples=1
    )
    state = get_default_state("question", schema_str="CREATE TABLE t (id INTEGER)")

    out = await node._aprocess(state)

    assert [(result.id, result.strategy, result.sql) for result in out.get("generation_results", [])] == [
        (0, "fast", "SELECT 'fast'"),
        (1, "slow", "SELECT 'slow'"),
    ]
    assert out.get("sql") == "SELECT 'fast'"


@pytest.mark.asyncio
async def test_generator_keeps_successful_sibling_after_strategy_failure() -> None:
    """Generator should retain successful candidates when another strategy fails."""
    node = _FailingGeneratorNode(
        config_manager=_ConfigManager(), strategies=["slow", "fail"], num_workers=1, num_samples=1
    )
    state = get_default_state("question", schema_str="CREATE TABLE t (id INTEGER)")

    out = await node._aprocess(state)

    assert node.slow_done.is_set()
    assert not node.slow_cancelled
    assert [(result.strategy, result.sql) for result in out.get("generation_results", [])] == [("slow", "SELECT 1")]
    assert out.get("sql") == "SELECT 1"


@pytest.mark.asyncio
async def test_generator_raises_only_after_all_strategy_tasks_fail() -> None:
    """Generator should wait for every task before surfacing an all-failed batch."""
    node = _AllFailingGeneratorNode(
        config_manager=_ConfigManager(), strategies=["slow-fail", "fast-fail"], num_workers=1, num_samples=1
    )
    state = get_default_state("question", schema_str="CREATE TABLE t (id INTEGER)")

    with pytest.raises(RuntimeError, match="failed"):
        await node._aprocess(state)

    assert node.calls == 2


@pytest.mark.asyncio
async def test_generator_cancels_children_when_generator_is_cancelled() -> None:
    """Cancelling Generator itself should cancel and await every child task."""
    node = _BlockingGeneratorNode(
        config_manager=_ConfigManager(), strategies=["first", "second"], num_workers=1, num_samples=1
    )
    state = get_default_state("question", schema_str="CREATE TABLE t (id INTEGER)")
    task = asyncio.create_task(node._aprocess(state))
    await asyncio.wait_for(node.all_started.wait(), timeout=0.2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert node.cancelled == 2


def test_context_dump_remains_synchronous() -> None:
    """LLM context dumps should remain synchronous and avoid a thread-pool wrapper."""
    assert not inspect.iscoroutinefunction(BaseNL2SQLNode._dump_llm_context)
    assert "_adump_llm_context" not in BaseNL2SQLNode.__dict__
    assert "to_thread" not in inspect.getsource(BaseNL2SQLNode.execute_with_llm)
    assert "to_thread" not in inspect.getsource(GeneratorNode.generate_with_llm)


@pytest.mark.asyncio
async def test_executor_keeps_service_lifecycle_in_one_worker_thread() -> None:
    """Executor should create, use, and close one service in the same worker thread."""
    node = ExecutorNode(config_manager=_ConfigManager())
    state = get_default_state("question")
    state["validation_results"] = [Result(id=0, sql="SELECT 1")]
    service = _ThreadTrackingService()

    with patch("dataagent.agents.nl2sql.nodes.executor.build_sql_service", return_value=service):
        await node._aprocess(state)

    assert len(service.thread_ids) == 3
    assert len(set(service.thread_ids)) == 1
    assert service.thread_ids[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_executor_cancellation_does_not_close_an_active_service() -> None:
    """Cancelling Executor should not close a service still used by its worker."""
    node = ExecutorNode(config_manager=_ConfigManager())
    state = get_default_state("question")
    state["validation_results"] = [Result(id=0, sql="SELECT 1")]
    service = _BlockingService()

    with patch("dataagent.agents.nl2sql.nodes.executor.build_sql_service", return_value=service):
        task = asyncio.create_task(node._aprocess(state))
        assert await asyncio.to_thread(service.started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        closed_before_release = service.closed
        service.release.set()
        assert await asyncio.to_thread(service.finished.wait, 1)
        assert await asyncio.to_thread(service.closed_event.wait, 1)

    assert not closed_before_release
    assert not service.closed_during_execute
    assert service.closed


@pytest.mark.asyncio
async def test_validator_keeps_explain_service_lifecycle_in_one_worker_thread() -> None:
    """Validator should create, use, and close an EXPLAIN service in one worker thread."""
    node = ValidatorNode(config_manager=_ConfigManager(), db_explain=True)
    service = _ThreadTrackingService()

    with patch("dataagent.agents.nl2sql.nodes.validator.build_sql_service", return_value=service):
        assert await node._validate_with_db_explain("SELECT 1") == []

    assert len(service.thread_ids) == 3
    assert len(set(service.thread_ids)) == 1
    assert service.thread_ids[0] != threading.get_ident()


def test_base_nl2sql_node_has_no_sync_fallback() -> None:
    """Base NL2SQL nodes should require concrete async processing implementations."""
    assert inspect.iscoroutinefunction(BaseNL2SQLNode.execute_with_llm)
    assert "_aprocess" not in BaseNL2SQLNode.__dict__


@pytest.mark.parametrize(
    "node_class",
    [GeneratorNode, PerceptorNode, UDNPerceptorNode, ValidatorNode, ReflectorNode, ExecutorNode, SelectorNode],
)
def test_nl2sql_nodes_use_async_processing_only(node_class) -> None:
    """Every NL2SQL node should expose only the async processing implementation."""
    assert inspect.iscoroutinefunction(node_class._aprocess)
    assert "_process" not in node_class.__dict__
