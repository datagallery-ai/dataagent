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
import os
from pathlib import Path

import pytest

from dataagent.common_utils.knowledge_base.memory import Memory
from dataagent.core.managers.prompt_manager import PromptTemplate


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
        self.mem.kb.storage.drop_table(self.mem.kb.index_texts)
        self.mem.kb.storage.drop_table(self.mem.kb.index_nodes)
        self.mem.kb.storage.drop_table(self.mem.kb.index_edges)

    def test_register_knowledge(self):
        json_path = os.path.join(self.path_prefix, "test_nodes_and_edges.json")

        md_path = os.path.join(self.path_prefix, "test_md.md")

        # case 1: file_type: markdown
        # no file_path
        assert not self.mem.register_knowledge(file_type="markdown")
        # wrong file_path
        assert not self.mem.register_knowledge(file_type="markdown", markdown_path="/wrong/path/to/file.md")
        # with file_path
        assert self.mem.register_knowledge(file_type="markdown", markdown_path=md_path)

        res_all = self.mem.kb.storage.query_all(table_name=self.mem.kb.index_texts)
        assert len(res_all) > 0

        # case 2: file_type: user_query
        user_prompt = "Find software for population genetics and association analysis"
        assert self.mem.register_knowledge(file_type="user_query", json_path=json_path, user_prompt=user_prompt)
        documents = self.mem.kb.storage.query_exact(
            table_name=self.mem.kb.index_texts,
            query_schema_list=["path"],
            query_text_list=[json_path],
            query_type="AND",
            topk=1,
        )
        assert len(documents) > 0
        assert documents[0]["path"] == json_path

    def test_query_knowledge(self):
        md_path = os.path.join(self.path_prefix, "test_md.md")
        assert (
            self.mem.query_knowledge(
                "text", query_schema="info", mode="fulltext", query_text="Fraud Prevention Team", topk=1
            )
            == []
        )

        self.mem.register_knowledge(file_type="markdown", markdown_path=md_path)

        assert (
            len(
                self.mem.query_knowledge(
                    "text", query_schema="info", mode="fulltext", query_text="Fraud Prevention Team", topk=1
                )
            )
            > 0
        )

    def test_remove_knowledge(self):
        md_path = os.path.join(self.path_prefix, "test_md.md")
        json_path = os.path.join(self.path_prefix, "test_nodes_and_edges.json")
        assert (
            self.mem.query_knowledge(
                "text", query_schema="info", mode="fulltext", query_text="Fraud Prevention Team", topk=1
            )
            == []
        )

        self.mem.register_knowledge(file_type="markdown", markdown_path=md_path)

        assert (
            len(
                self.mem.query_knowledge(
                    "text", query_schema="info", mode="fulltext", query_text="Fraud Prevention Team", topk=1
                )
            )
            > 0
        )

        self.mem.remove_knowledge("document", file_path=md_path)

        assert (
            self.mem.query_knowledge(
                "text", query_schema="info", mode="fulltext", query_text="Fraud Prevention Team", topk=1
            )
            == []
        )

        assert (
            self.mem.query_knowledge("graph_node", query_schema="path", mode="fulltext", query_text=json_path, topk=1)
            == []
        )

        self.mem.register_knowledge(
            file_type="user_query",
            json_path=json_path,
            user_prompt="Find software for population genetics and association analysis",
        )
        assert (
            len(
                self.mem.query_knowledge(
                    "text", query_schema="info", mode="fulltext", query_text="population genetics", topk=1
                )
            )
            == 1
        )

        assert (
            len(
                self.mem.query_knowledge(
                    "graph_node", query_schema="path", mode="fulltext", query_text=json_path, topk=1
                )
            )
            == 1
        )

        self.mem.remove_knowledge("graph", file_path=json_path)
        self.mem.remove_knowledge("document", file_path=json_path)

        assert (
            self.mem.query_knowledge(
                "text", query_schema="info", mode="fulltext", query_text="population genetics", topk=1
            )
            == []
        )

        assert (
            self.mem.query_knowledge("graph_node", query_schema="path", mode="fulltext", query_text=json_path, topk=1)
            == []
        )
