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
import contextlib
import tempfile
import time

import pandas as pd
import pytest

from dataagent.core.managers.action_manager.manager import ToolManager


@pytest.mark.asyncio
async def test_connection_performance():
    """测试连接性能"""
    print("=" * 60)
    print("MCP connection performance test")
    print("=" * 60)

    tm = ToolManager()

    # 创建测试数据
    print("\nPreparing test data...")
    data = {"id": list(range(10)), "name": [f"Item_{i}" for i in range(10)], "value": list(range(10, 20))}
    df = pd.DataFrame(data)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        csv_path = f.name
        df.to_csv(csv_path, index=False)
        json_path = csv_path.replace(".csv", ".json")

    print(f"Test data: {csv_path}")

    try:
        # 注册服务器并发现工具
        print("Discovering tools...")
        tm.register_mcp_server(
            server_id="perf_test",
            transport_type="stdio",
            config={"command": "npx", "args": ["-y", "mcp-server-data-analysis"]},
        )
        start_time = time.time()
        discovered = await tm.discover_mcp_tools("perf_test")
        discover_time = time.time() - start_time
        print(f"Discovery time: {discover_time:.3f}s, found: {discovered}")

        # 测试多次调用的性能
        print("\nRunning multiple tool calls...")
        num_calls = 10
        call_times = []

        for i in range(num_calls):
            start_time = time.time()
            result = await tm.acall("perf_test.statistical_analyzer", csv_path=csv_path, json_path=json_path)
            call_time = time.time() - start_time
            call_times.append(call_time)

            print(f"  Call {i + 1:2d}: {call_time:.3f}s {'OK' if result.success else 'FAIL'}")

        # 统计分析
        print("\nPerformance stats:")
        print(f"  Total calls: {num_calls}")
        print(f"  Avg time: {sum(call_times) / len(call_times):.3f}s")
        print(f"  Min time: {min(call_times):.3f}s")
        print(f"  Max time: {max(call_times):.3f}s")
        print(f"  Total time: {sum(call_times):.3f}s")

        # 连接复用效果分析
        if len(call_times) > 1:
            first_call = call_times[0]
            subsequent_calls = call_times[1:]
            avg_subsequent = sum(subsequent_calls) / len(subsequent_calls)

            if avg_subsequent < first_call:
                improvement = ((first_call - avg_subsequent) / first_call) * 100
                print(f"  Connection reuse: subsequent calls avg {improvement:.1f}% faster")
            else:
                print("  Connection reuse: performance stable")

        print("\nPerformance test complete!")

    finally:
        await tm.cleanup()
        import os

        with contextlib.suppress(Exception):
            os.unlink(csv_path)


@pytest.mark.asyncio
async def test_concurrent_calls():
    """测试并发调用性能"""
    print("\n" + "=" * 60)
    print("MCP concurrent call test")
    print("=" * 60)

    tm = ToolManager()

    # 准备数据
    data = {"id": [1, 2, 3], "name": ["X", "Y", "Z"]}
    df = pd.DataFrame(data)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        csv_path = f.name
        df.to_csv(csv_path, index=False)
        json_path = csv_path.replace(".csv", ".json")

    try:
        tm.register_mcp_server(
            server_id="concurrent_test",
            transport_type="stdio",
            config={"command": "npx", "args": ["-y", "mcp-server-data-analysis"]},
        )
        await tm.discover_mcp_tools("concurrent_test")

        # 并发调用测试
        async def single_call(call_id):
            start_time = time.time()
            result = await tm.acall("concurrent_test.statistical_analyzer", csv_path=csv_path, json_path=json_path)
            elapsed = time.time() - start_time
            return call_id, elapsed, result.success

        print("Running 5 concurrent calls...")
        start_time = time.time()

        tasks = [single_call(i) for i in range(5)]
        results = await asyncio.gather(*tasks)

        total_time = time.time() - start_time

        print("Concurrent calls complete:")
        for call_id, elapsed, success in results:
            print(f"  Call {call_id + 1}: {elapsed:.3f}s {'OK' if success else 'FAIL'}")

        print("\nConcurrent performance:")
        print(f"  Total time: {total_time:.3f}s")
        print(f"  Avg single call: {sum(r[1] for r in results) / len(results):.3f}s")
        print(f"  Serial estimate: {sum(r[1] for r in results):.3f}s")

        if total_time < sum(r[1] for r in results):
            speedup = sum(r[1] for r in results) / total_time
            print(f"  Speedup: {speedup:.2f}x")

    finally:
        await tm.cleanup()
        import os

        with contextlib.suppress(Exception):
            os.unlink(csv_path)
