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
            {"changping02.orders": {"table_description": "订单表"}},
            {"changping02.users": {"table_description_enhanced": "用户表(增强)", "table_description": "用户表"}},
        ]

    def get_table_columns_info(self, table_name: str, *, limit: int) -> dict[str, Any]:
        self.columns_calls.append(table_name)
        if table_name == "changping02.orders":
            return {
                "changping02.orders.order_id": {"column_short_description": "订单ID", "value_type": "string"},
                "changping02.orders.user_id": {"column_short_description": "用户ID", "value_type": "string"},
            }
        return {"changping02.users.user_id": {"column_short_description": "用户ID", "value_type": "string"}}

    def get_joinable_tables(self, table_names: list[str], *, limit: int) -> list[dict[str, Any]]:
        self.joinable_calls.append(list(table_names))
        return [
            {
                "src": "changping02.orders.user_id",
                "target_column": ["changping02.users.user_id"],
                "expression": "orders.user_id = users.user_id",
                "rel_type": "many-to-one",
            }
        ]


def test_get_ontology_description_uses_rest_apis(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeClient()
    monkeypatch.setattr(ontology_query, "_client", lambda ctx: fake_client)

    ctx = _FakeContext({"DATABASE.db_id": "changping02"})
    result = ontology_query.get_ontology_description(_tool_context=ctx)

    assert fake_client.table_list_calls == ["changping02"]
    assert sorted(fake_client.columns_calls) == ["changping02.orders", "changping02.users"]
    # Two tables fit in one joinable batch.
    assert fake_client.joinable_calls == [["changping02.orders", "changping02.users"]]

    original = result["original_msg"]
    # Single-database scene: the ``changping02.`` prefix is stripped in the
    # rendered names, leaving bare table names.
    assert "changping02" not in original
    assert '"orders"' in original
    assert '"users"' in original
    assert "用户表(增强)" in original  # enhanced description preferred
    assert "orders关联到users" in original
    assert "orders.user_id = users.user_id" in original
    # value_type is no longer surfaced; properties carry only name + description.
    assert "value_type" not in original

    assert result["frontend_msg"] == (
        "已从语义层服务加载本体 changping02 描述信息，本体中共包括2种实体，1种关系，它们的具体schema也已经被加载。"
    )


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
            if table_name == "changping02.orders":
                raise requests.RequestException("boom")
            return {"changping02.users.user_id": {"column_short_description": "用户ID"}}

    fake = _Client()
    monkeypatch.setattr(ontology_query, "_client", lambda ctx: fake)
    result = ontology_query.get_ontology_description(_tool_context=_FakeContext({"DATABASE.db_id": "changping02"}))

    # Failing table still renders (with empty properties, prefix stripped); no
    # exception bubbles up.
    assert '"orders"' in result["original_msg"]
    assert "共包括2种实体" in result["frontend_msg"]


def test_service_error_degrades_gracefully(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client(_FakeClient):
        def get_table_list(self, database_name: str, *, limit: int) -> list[dict[str, Any]]:
            raise requests.RequestException("service down")

    monkeypatch.setattr(ontology_query, "_client", lambda ctx: _Client())
    result = ontology_query.get_ontology_description(_tool_context=_FakeContext({"DATABASE.db_id": "changping02"}))

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
