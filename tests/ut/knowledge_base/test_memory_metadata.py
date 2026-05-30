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
import json
import os
from pathlib import Path

import pytest

from dataagent.common_utils.knowledge_base.memory import Memory


class TestMetadataManagement:
    @pytest.fixture(autouse=True)
    def setup(self, kb_ut_config_manager):
        self.path_prefix = os.path.join(
            Path(__file__).parents[1],
            "knowledge_base",
            "ut_data",
        )
        self.mem = Memory(
            path_prefix=self.path_prefix,
            index_prefix="pytest_memory1224",
            config_manager=kb_ut_config_manager,
        )
        yield

        self.mem.tool.storage.drop_table(self.mem.tool.index_edges)
        self.mem.tool.storage.drop_table(self.mem.tool.index_nodes)
        self.mem.metadata.storage.drop_table(self.mem.metadata.index_edges)
        self.mem.metadata.storage.drop_table(self.mem.metadata.index_nodes)

    def test_register_table(self):
        table_metadata_path = os.path.join(self.path_prefix, "test_table_metadata.json")
        with open(table_metadata_path, encoding="utf-8") as f:
            test_table_metadata = json.load(f)
        for path, table_metadata in test_table_metadata.items():
            full_path = os.path.join(self.path_prefix, path)
            self.mem.register_table(table_path=full_path, provided_meta=table_metadata)
        edges = self.mem.kb.storage.query_all(table_name=self.mem.metadata.index_edges)
        nodes = self.mem.kb.storage.query_all(table_name=self.mem.metadata.index_nodes)
        assert len(nodes) > 0
        assert len(edges) > 0
        self.mem.metadata.storage.drop_table(self.mem.metadata.index_edges)
        self.mem.metadata.storage.drop_table(self.mem.metadata.index_nodes)
        assert not self.mem.metadata.storage.es.indices.exists(index=self.mem.metadata.index_edges)
        assert not self.mem.metadata.storage.es.indices.exists(index=self.mem.metadata.index_nodes)

    def test_query_table(self):
        table_metadata_path = os.path.join(self.path_prefix, "test_table_metadata.json")
        with open(table_metadata_path, encoding="utf-8") as f:
            test_table_metadata = json.load(f)
        for path, table_metadata in test_table_metadata.items():
            full_path = os.path.join(self.path_prefix, path)
            self.mem.register_table(table_path=full_path, provided_meta=table_metadata)
        assert (
            len(
                self.mem.query_table(
                    "column", mode="fulltext", query_schema="label", query_text="filename_feature", topk=1
                )
            )
            > 0
        )

    def test_remove_table(self):
        table_metadata_path = os.path.join(self.path_prefix, "test_table_metadata.json")

        with open(table_metadata_path, encoding="utf-8") as f:
            test_table_metadata = json.load(f)
        for path, table_metadata in test_table_metadata.items():
            full_path = os.path.join(self.path_prefix, path)
            self.mem.register_table(table_path=full_path, provided_meta=table_metadata)
        assert (
            len(
                self.mem.query_table(
                    "column", mode="fulltext", query_schema="label", query_text="filename_feature", topk=1
                )
            )
            == 1
        )

        for path, _ in test_table_metadata.items():
            full_path = os.path.join(self.path_prefix, path)
            self.mem.remove_table(table_path=full_path)

        assert (
            self.mem.query_table("column", mode="fulltext", query_schema="label", query_text="filename_feature", topk=1)
            == []
        )

    def test_register_tool(self):
        assert self.mem.register_tool(
            toolname="test_tool",
            provided_meta={
                "type": "mcp_tool",
                "description": "A test tool for unit testing.",
                "parameters": "query(str): The input query string.",
                "output": "response(str): The output response string.",
                "tag": "",
                "description_for_display": "A tool for testing purposes.",
                "version": "1.0",
            },
        )

        assert len(self.mem.tool.storage.query_all(table_name=self.mem.tool.index_nodes)) == 1
        assert len(self.mem.tool.storage.query_all(table_name=self.mem.tool.index_edges)) == 0

    def test_query_tool(self):
        assert self.mem.query_tool(mode="fulltext", query_schema="label", query_text="test_tool", topk=1) == []
        self.mem.register_tool(
            toolname="test_tool",
            provided_meta={
                "type": "mcp_tool",
                "description": "A test tool for unit testing.",
                "parameters": "query(str): The input query string.",
                "output": "response(str): The output response string.",
                "tag": "",
                "description_for_display": "A tool for testing purposes.",
                "version": "1.0",
            },
        )
        assert len(self.mem.query_tool(mode="fulltext", query_schema="label", query_text="test_tool", topk=1)) == 1

    def test_remove_tool(self):
        assert self.mem.query_tool(mode="fulltext", query_schema="label", query_text="test_tool", topk=1) == []
        self.mem.register_tool(
            toolname="test_tool",
            provided_meta={
                "type": "mcp_tool",
                "description": "A test tool for unit testing.",
                "parameters": "query(str): The input query string.",
                "output": "response(str): The output response string.",
                "tag": "",
                "description_for_display": "A tool for testing purposes.",
                "version": "1.0",
            },
        )
        assert len(self.mem.query_tool(mode="fulltext", query_schema="label", query_text="test_tool", topk=1)) == 1
        self.mem.remove_tool(toolname="test_tool")
        assert self.mem.query_tool(mode="fulltext", query_schema="label", query_text="test_tool", topk=1) == []
