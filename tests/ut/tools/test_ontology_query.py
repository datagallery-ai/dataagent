from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from dataagent.actions.tools.semantic_tool import ontology_query


class _FakeConfigManager:
    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


class _FakeContext:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config_manager = _FakeConfigManager(config)


class _FakeClient:
    """Mimics the SemanticServiceClient basic-retrieval REST surface."""

    def __init__(self) -> None:
        self.table_list_calls: list[str] = []
        self.columns_calls: list[str] = []
        self.joinable_calls: list[list[str]] = []

    def get_table_list(self, database_name: str, *, limit: int) -> list[dict[str, Any]]:
        self.table_list_calls.append(database_name)
        return [
            {"bio_lab.orders": {"table_description": "订单表"}},
            {"bio_lab.users": {"table_description_enhanced": "用户表(增强)", "table_description": "用户表"}},
        ]

    def get_table_columns_info(self, table_name: str, *, limit: int) -> dict[str, Any]:
        self.columns_calls.append(table_name)
        if table_name == "bio_lab.orders":
            return {
                "bio_lab.orders.order_id": {"column_short_description": "订单ID", "value_type": "string"},
                "bio_lab.orders.user_id": {"column_short_description": "用户ID", "value_type": "string"},
            }
        return {"bio_lab.users.user_id": {"column_short_description": "用户ID", "value_type": "string"}}

    def get_joinable_tables(self, table_names: list[str], *, limit: int) -> list[dict[str, Any]]:
        self.joinable_calls.append(list(table_names))
        return [
            {
                "src": "bio_lab.orders.user_id",
                "target_column": ["bio_lab.users.user_id"],
                "expression": "orders.user_id = users.user_id",
                "rel_type": "many-to-one",
            }
        ]


def test_get_ontology_description_uses_rest_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeClient()
    monkeypatch.setattr(ontology_query, "_client", lambda ctx: fake_client)

    ctx = _FakeContext({"DATABASE.db_id": "bio_lab"})
    result = ontology_query.get_ontology_description(_tool_context=ctx)

    assert fake_client.table_list_calls == ["bio_lab"]
    assert sorted(fake_client.columns_calls) == ["bio_lab.orders", "bio_lab.users"]
    # Two tables fit in one joinable batch.
    assert fake_client.joinable_calls == [["bio_lab.orders", "bio_lab.users"]]

    original = result["original_msg"]
    # Single-database scene: the ``bio_lab.`` prefix is stripped in the
    # rendered names, leaving bare table names.
    assert "bio_lab" not in original
    assert '"orders"' in original
    assert '"users"' in original
    assert "用户表(增强)" in original  # enhanced description preferred
    assert "orders关联到users" in original
    assert "orders.user_id = users.user_id" in original
    # value_type is no longer surfaced; properties carry only name + description.
    assert "value_type" not in original

    assert result["frontend_msg"] == (
        "已从语义层服务加载本体 bio_lab 描述信息，本体中共包括2种实体，1种关系，它们的具体schema也已经被加载。"
    )


def test_get_relevant_ontology_description_uses_semantic_retrieve(monkeypatch: pytest.MonkeyPatch) -> None:
    class _SemanticRetrieveClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.semantic_retrieve_calls: list[str] = []

        def semantic_retrieve(self, query: str) -> dict[str, Any]:
            self.semantic_retrieve_calls.append(query)
            return {
                "query": query,
                "queryUnderstanding": {
                    "intent": "detail_lookup",
                    "subGoals": ["获取 IC50 结果"],
                    "concepts": ["中和实验", "IC50"],
                    "timeGrain": None,
                    "filters": [],
                    "termMappings": [],
                },
                "metricContext": [],
                "dataAccessPlan": {
                    "tables": [
                        {
                            "db": "bio_lab",
                            "table": "neutralization_ic50_fit_data",
                            "description": "IC50拟合数据表",
                            "purpose": "存储拟合结果",
                            "columns": [
                                {
                                    "name": "id",
                                    "description": "样本ID",
                                    "isPrimaryKey": True,
                                    "isForeignKey": True,
                                },
                                {
                                    "name": "ic50",
                                    "description": "拟合得到的IC50值",
                                    "valueType": "double",
                                    "matchedValues": [{"value": "LOW", "description": "低IC50"}],
                                },
                            ],
                        },
                        {
                            "db": "bio_lab",
                            "table": "neutralization_ic50_fits",
                            "description": "IC50拟合实验表",
                            "purpose": "连接输入数据与输出 IC50 结果",
                            "columns": [
                                {"name": "output_data_id", "description": "输出拟合数据样本"},
                            ],
                        },
                    ],
                    "joinPaths": [
                        {
                            "left": "bio_lab.neutralization_ic50_fits",
                            "right": "bio_lab.neutralization_ic50_fit_data",
                            "on": (
                                '["bio_lab.neutralization_ic50_fits.output_data_id = '
                                'bio_lab.neutralization_ic50_fit_data.id"]'
                            ),
                            "cardinality": None,
                        }
                    ],
                },
                "knowledgeEvidence": [{"name": "IC50说明", "rawText": "IC50越低，抑制效果越好"}],
                "sqlExamples": [{"name": "IC50结果查询", "expression": "select ic50 from t", "intent": "查询IC50"}],
                "answerGuidance": "从 neutralization_ic50_fit_data.ic50 取结果。",
                "diagnostic": {"llmCalls": 2},
            }

    fake_client = _SemanticRetrieveClient()
    monkeypatch.setattr(ontology_query, "_client", lambda ctx: fake_client)

    result = ontology_query.get_relevant_ontology_description(
        query="查找中和实验的 IC50 结果",
        _tool_context=_FakeContext({"DATABASE.db_id": "bio_lab"}),
    )

    assert fake_client.semantic_retrieve_calls == ["查找中和实验的 IC50 结果"]
    assert fake_client.table_list_calls == []
    assert fake_client.columns_calls == []
    assert fake_client.joinable_calls == []

    original = result["original_msg"]
    assert "对本体查询结果如下" in original
    assert "本体目前包含以下几种类型实体" in original
    assert "每种实体的描述和属性定义如下" in original
    assert "实体之间有以下几种类型的关联" in original
    assert "不是全量本体" not in original
    assert "用户查询" not in original
    assert "相关实体/表" not in original
    assert '"neutralization_ic50_fit_data"' in original
    assert "bio_lab." not in original
    assert '"queryUnderstanding"' not in original
    assert '"dataAccessPlan"' not in original
    assert '"answerGuidance"' not in original
    assert '"recall_reason"' not in original
    assert '"property_type"' not in original
    assert '"flags"' not in original
    assert "neutralization_ic50_fits关联到neutralization_ic50_fit_data" in original
    assert "neutralization_ic50_fits.output_data_id = neutralization_ic50_fit_data.id" in original
    assert "IC50说明" not in original
    assert "IC50结果查询" not in original
    assert "从 neutralization_ic50_fit_data.ic50 取结果" not in original
    assert set(result) == {"original_msg", "frontend_msg"}
    assert result["frontend_msg"] == (
        "已从语义层服务按查询加载相关本体 bio_lab 描述信息，本体中共包括2种实体，1种关系，它们的具体schema也已经被加载。"
    )


def test_relevant_ontology_empty_tables_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _EmptyRetrieveClient(_FakeClient):
        def semantic_retrieve(self, query: str) -> dict[str, Any]:
            return {
                "query": query,
                "dataAccessPlan": {
                    "tables": [],
                    "joinPaths": [],
                },
            }

    monkeypatch.setattr(ontology_query, "_client", lambda ctx: _EmptyRetrieveClient())

    result = ontology_query.get_relevant_ontology_description(
        query="查询不存在的业务表",
        _tool_context=_FakeContext({"DATABASE.db_id": "bio_lab"}),
    )

    assert result["original_msg"] == result["frontend_msg"]
    assert "semantic/retrieve 未召回相关表" in result["original_msg"]
    assert "请不要基于空本体继续构造 SQL" in result["original_msg"]
    assert "可以根据以上信息" not in result["original_msg"]
    assert "构造查询条件" not in result["original_msg"]


def test_relevant_ontology_service_error_does_not_render_empty_sql_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingRetrieveClient(_FakeClient):
        def semantic_retrieve(self, query: str) -> dict[str, Any]:
            raise requests.RequestException("semantic service down")

    monkeypatch.setattr(ontology_query, "_client", lambda ctx: _FailingRetrieveClient())

    result = ontology_query.get_relevant_ontology_description(
        query="查询订单",
        _tool_context=_FakeContext({"DATABASE.db_id": "bio_lab"}),
    )

    assert result["original_msg"] == result["frontend_msg"]
    assert "相关语义检索失败" in result["original_msg"]
    assert "semantic service down" in result["original_msg"]
    assert "请不要基于空本体继续构造 SQL" in result["original_msg"]
    assert "可以根据以上信息" not in result["original_msg"]
    assert "构造查询条件" not in result["original_msg"]


def test_relevant_ontology_invalid_bundle_does_not_render_empty_sql_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _InvalidRetrieveClient(_FakeClient):
        def semantic_retrieve(self, query: str) -> list[Any]:  # type: ignore[override]
            return []

    monkeypatch.setattr(ontology_query, "_client", lambda ctx: _InvalidRetrieveClient())

    result = ontology_query.get_relevant_ontology_description(
        query="查询订单",
        _tool_context=_FakeContext({"DATABASE.db_id": "bio_lab"}),
    )

    assert result["original_msg"] == result["frontend_msg"]
    assert "semantic/retrieve 返回格式无效" in result["original_msg"]
    assert "请不要基于空本体继续构造 SQL" in result["original_msg"]
    assert "可以根据以上信息" not in result["original_msg"]
    assert "构造查询条件" not in result["original_msg"]


def test_multiple_databases_are_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    class _MultiDbClient(_FakeClient):
        def get_table_list(self, database_name: str, *, limit: int) -> list[dict[str, Any]]:
            self.table_list_calls.append(database_name)
            return [{f"{database_name}.t": {"table_description": f"{database_name} 表"}}]

        def get_table_columns_info(self, table_name: str, *, limit: int) -> dict[str, Any]:
            self.columns_calls.append(table_name)
            return {f"{table_name}.c": {"column_short_description": "c"}}

        def get_joinable_tables(self, table_names: list[str], *, limit: int) -> list[dict[str, Any]]:
            self.joinable_calls.append(list(table_names))
            return []

    fake = _MultiDbClient()
    monkeypatch.setattr(ontology_query, "_client", lambda ctx: fake)
    result = ontology_query.get_ontology_description(_tool_context=_FakeContext({"DATABASE.db_id": ["db1", "db2"]}))

    assert fake.table_list_calls == ["db1", "db2"]
    # Multi-database scene keeps the ``<db>.`` qualifier: stripping it would
    # collapse ``db1.t`` and ``db2.t`` into an ambiguous ``t``/``t`` pair.
    assert "db1.t" in result["original_msg"]
    assert "db2.t" in result["original_msg"]
    assert "共包括2种实体" in result["frontend_msg"]


def test_relevant_ontology_keeps_db_prefixes_for_multiple_databases(monkeypatch: pytest.MonkeyPatch) -> None:
    class _MultiDbRetrieveClient(_FakeClient):
        def semantic_retrieve(self, query: str) -> dict[str, Any]:
            return {
                "dataAccessPlan": {
                    "tables": [
                        {
                            "db": "db1",
                            "table": "t",
                            "description": "db1 表",
                            "columns": [{"name": "id", "description": "db1 id"}],
                        },
                        {
                            "db": "db2",
                            "table": "t",
                            "description": "db2 表",
                            "columns": [{"name": "id", "description": "db2 id"}],
                        },
                    ],
                    "joinPaths": [
                        {
                            "left": "db1.t",
                            "right": "db2.t",
                            "on": "db1.t.id = db2.t.id",
                        }
                    ],
                }
            }

    monkeypatch.setattr(ontology_query, "_client", lambda ctx: _MultiDbRetrieveClient())
    result = ontology_query.get_relevant_ontology_description(
        query="查询多库同名表",
        _tool_context=_FakeContext({"DATABASE.db_id": ["db1", "db2"]}),
    )

    original = result["original_msg"]
    assert '"entity_name": "db1.t"' in original
    assert '"entity_name": "db2.t"' in original
    assert '"entity_name": "t"' not in original
    assert "db1.t关联到db2.t" in original
    assert "db1.t.id = db2.t.id" in original


def test_relevant_ontology_without_database_keeps_qualified_names(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoDbRetrieveClient(_FakeClient):
        def semantic_retrieve(self, query: str) -> dict[str, Any]:
            return {
                "dataAccessPlan": {
                    "tables": [
                        {
                            "db": "bio_lab",
                            "table": "orders",
                            "description": "订单表",
                            "columns": [{"name": "id", "description": "订单ID"}],
                        }
                    ],
                    "joinPaths": [],
                }
            }

    monkeypatch.setattr(ontology_query, "_client", lambda ctx: _NoDbRetrieveClient())
    result = ontology_query.get_relevant_ontology_description(
        query="查询订单",
        _tool_context=_FakeContext({}),
    )

    assert '"entity_name": "bio_lab.orders"' in result["original_msg"]
    assert '"entity_name": "orders"' not in result["original_msg"]
    assert "按查询加载相关本体 default 描述信息" in result["frontend_msg"]


def test_multiple_join_conditions_aggregated() -> None:
    class _Client:
        def get_joinable_tables(self, table_names: list[str], *, limit: int) -> list[dict[str, Any]]:
            return [
                {"src": "db.a.x", "target_column": ["db.b.x"], "expression": "a.x = b.x", "rel_type": "1-n"},
                {"src": "db.a.y", "target_column": ["db.b.y"], "expression": "a.y = b.y", "rel_type": "1-n"},
            ]

    relations = ontology_query._fetch_relations(_Client(), ["db.a", "db.b"])
    assert len(relations) == 1
    assert relations[0]["join_condition"] == "a.x = b.x；a.y = b.y"


def test_dangling_relation_filtered() -> None:
    class _Client:
        def get_joinable_tables(self, table_names: list[str], *, limit: int) -> list[dict[str, Any]]:
            return [
                {"src": "db.a.x", "target_column": ["db.outside.x"], "expression": "a.x = outside.x"},
            ]

    # db.outside is not part of the scene's entity set -> relation dropped.
    assert ontology_query._fetch_relations(_Client(), ["db.a", "db.b"]) == []


def test_joinable_requests_are_batched() -> None:
    class _Client:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        def get_joinable_tables(self, table_names: list[str], *, limit: int) -> list[dict[str, Any]]:
            self.batch_sizes.append(len(table_names))
            return []

    client = _Client()
    tables = [f"db.t{i}" for i in range(120)]
    ontology_query._fetch_relations(client, tables)
    # 120 tables / batch 50 -> 50, 50, 20
    assert client.batch_sizes == [50, 50, 20]


def test_joinable_non_list_response_is_tolerated() -> None:
    class _Client:
        def get_joinable_tables(self, table_names: list[str], *, limit: int) -> Any:
            return None

    assert ontology_query._fetch_relations(_Client(), ["db.a"]) == []


def test_column_fetch_error_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def get_table_columns_info(self, table_name: str, *, limit: int) -> dict[str, Any]:
            self.columns_calls.append(table_name)
            if table_name == "bio_lab.orders":
                raise requests.RequestException("boom")
            return {"bio_lab.users.user_id": {"column_short_description": "用户ID"}}

    fake = _Client()
    monkeypatch.setattr(ontology_query, "_client", lambda ctx: fake)
    result = ontology_query.get_ontology_description(_tool_context=_FakeContext({"DATABASE.db_id": "bio_lab"}))

    # Failing table still renders (with empty properties, prefix stripped); no
    # exception bubbles up.
    assert '"orders"' in result["original_msg"]
    assert "共包括2种实体" in result["frontend_msg"]


def test_service_error_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def get_table_list(self, database_name: str, *, limit: int) -> list[dict[str, Any]]:
            raise requests.RequestException("service down")

    monkeypatch.setattr(ontology_query, "_client", lambda ctx: _Client())
    result = ontology_query.get_ontology_description(_tool_context=_FakeContext({"DATABASE.db_id": "bio_lab"}))

    assert "加载失败" in result["frontend_msg"]
    assert json.loads(result["original_msg"].split("本体目前包含以下几种类型实体：")[1].split("\n\n")[0]) == []


def test_get_ontology_description_missing_database_raises() -> None:
    with pytest.raises(ValueError, match="Ontology database is required"):
        ontology_query.get_ontology_description(_tool_context=_FakeContext({}))


def test_table_key_two_segment_heuristic() -> None:
    # Three-segment column names collapse to their owning ``db.table``.
    assert ontology_query._table_key("db.table.col") == "db.table"
    # Two-segment table names are returned as-is.
    assert ontology_query._table_key("db.table") == "db.table"
    assert ontology_query._table_key("") == ""


def test_empty_entities_still_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    class _EmptyClient(_FakeClient):
        def get_table_list(self, database_name: str, *, limit: int) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(ontology_query, "_client", lambda ctx: _EmptyClient())
    result = ontology_query.get_ontology_description(_tool_context=_FakeContext({"DATABASE.db_id": "s"}))
    assert json.loads(result["original_msg"].split("本体目前包含以下几种类型实体：")[1].split("\n\n")[0]) == []
