"""Tests for DataLink data models."""

from datalink.models import (
    ColumnInfo,
    ColumnNode,
    ColumnProfile,
    ConceptNode,
    DatasourceConfig,
    DatasourceInfo,
    DatasourceType,
    Edge,
    EdgeType,
    EntityNode,
    ForeignKeyInfo,
    NodeType,
    TableInfo,
    TableNode,
)


class TestNodeModels:
    """Test node model creation and validation."""

    def test_column_node_defaults(self):
        node = ColumnNode(
            id="column:postgres:orders:customer_id",
            name="customer_id",
            table_id="table:postgres:orders",
            dtype="integer",
        )
        assert node.type == NodeType.COLUMN
        assert node.semantic_type == ""
        assert node.comment == ""
        assert node.profile_id == ""

    def test_column_node_with_all_fields(self):
        node = ColumnNode(
            id="column:postgres:orders:customer_id",
            name="customer_id",
            table_id="table:postgres:orders",
            dtype="integer",
            semantic_type="person_identifier",
            comment="Foreign key to users table",
            profile_id="profile:postgres:orders:customer_id",
            properties={"is_indexed": True},
        )
        assert node.semantic_type == "person_identifier"
        assert node.comment == "Foreign key to users table"

    def test_table_node(self):
        node = TableNode(
            id="table:postgres:orders",
            name="orders",
            source="postgresql://localhost/mydb",
            row_count=10000,
            column_ids=[
                "column:postgres:orders:order_id",
                "column:postgres:orders:customer_id",
                "column:postgres:orders:amount",
            ],
        )
        assert node.type == NodeType.TABLE
        assert node.row_count == 10000
        assert len(node.column_ids) == 3

    def test_concept_node(self):
        node = ConceptNode(
            id="concept:revenue",
            name="revenue",
            description="Total monetary income from sales",
            unit="USD",
            dimension="monetary",
        )
        assert node.type == NodeType.CONCEPT
        assert node.unit == "USD"

    def test_entity_node(self):
        node = EntityNode(
            id="entity:customer",
            name="customer",
            description="A person who purchases products",
        )
        assert node.type == NodeType.ENTITY
        assert node.description == "A person who purchases products"


class TestEdgeModels:
    """Test edge model creation and validation."""

    def test_explicit_edge(self):
        edge = Edge(
            id="edge:fk:orders_customer_id_users_id",
            source_id="column:postgres:orders:customer_id",
            target_id="column:postgres:users:id",
            type=EdgeType.FOREIGN_KEY,
        )
        assert edge.confidence == 1.0
        assert edge.is_explicit
        assert not edge.is_inferred

    def test_inferred_edge_with_confidence(self):
        edge = Edge(
            id="edge:joinable:orders_customer_id_transactions_user_id",
            source_id="column:postgres:orders:customer_id",
            target_id="column:postgres:transactions:user_id",
            type=EdgeType.JOINABLE,
            confidence=0.85,
        )
        assert edge.confidence == 0.85
        assert edge.is_inferred
        assert not edge.is_explicit

    def test_confidence_bounds(self):
        # Valid confidence
        Edge(id="e:1", source_id="a", target_id="b", type=EdgeType.JOINABLE, confidence=0.0)
        Edge(id="e:2", source_id="a", target_id="b", type=EdgeType.JOINABLE, confidence=1.0)

    def test_contains_edge(self):
        edge = Edge(
            id="edge:contains:orders_customer_id",
            source_id="table:postgres:orders",
            target_id="column:postgres:orders:customer_id",
            type=EdgeType.CONTAINS,
        )
        assert edge.type == EdgeType.CONTAINS

    def test_represents_edge(self):
        edge = Edge(
            id="edge:represents:orders_amount_revenue",
            source_id="column:postgres:orders:amount",
            target_id="concept:revenue",
            type=EdgeType.REPRESENTS,
            confidence=0.9,
        )
        assert edge.type == EdgeType.REPRESENTS

    def test_edge_properties(self):
        edge = Edge(
            id="edge:corr:1",
            source_id="col:a",
            target_id="col:b",
            type=EdgeType.CORRELATED,
            confidence=0.78,
            properties={"coefficient": 0.78, "method": "pearson"},
        )
        assert edge.properties["coefficient"] == 0.78


class TestColumnProfile:
    """Test column profile model."""

    def test_basic_profile(self):
        profile = ColumnProfile(
            id="profile:orders:customer_id",
            column_id="column:postgres:orders:customer_id",
            dtype="integer",
            null_rate=0.02,
            cardinality=8,
            unique_rate=0.8,
            total_count=10,
            sample_values=[1, 2, 3, 5],
        )
        assert profile.dtype == "integer"
        assert profile.null_rate == 0.02
        assert profile.semantic_type == "unknown"

    def test_numeric_profile(self):
        profile = ColumnProfile(
            id="profile:orders:amount",
            column_id="column:postgres:orders:amount",
            dtype="float",
            min_value=45.3,
            max_value=410.0,
            mean_value=156.37,
            std_value=112.5,
            semantic_type="monetary_value",
        )
        assert profile.min_value == 45.3
        assert profile.semantic_type == "monetary_value"

    def test_string_profile(self):
        profile = ColumnProfile(
            id="profile:users:email",
            column_id="column:postgres:users:email",
            dtype="string",
            min_length=15,
            max_length=20,
            avg_length=17.5,
            value_patterns=["email_pattern"],
            sample_values=["alice@example.com", "bob@example.com"],
        )
        assert profile.min_length == 15
        assert "email_pattern" in profile.value_patterns


class TestDatasourceModels:
    """Test datasource configuration and info models."""

    def test_datasource_config_csv(self):
        config = DatasourceConfig(
            type=DatasourceType.CSV,
            name="test_csv",
            path="/data/users.csv",
        )
        assert config.type == DatasourceType.CSV
        assert config.sample_size == 1000

    def test_datasource_config_database(self):
        config = DatasourceConfig(
            type=DatasourceType.DATABASE,
            name="mydb",
            connection_string="postgresql://user:pass@localhost/mydb",
        )
        assert config.type == DatasourceType.DATABASE

    def test_column_info(self):
        col = ColumnInfo(
            name="customer_id",
            dtype="integer",
            nullable=False,
            is_primary_key=False,
            comment="FK to users.id",
        )
        assert col.comment == "FK to users.id"

    def test_table_info_with_fk(self):
        fk = ForeignKeyInfo(
            constraint_name="fk_orders_customer",
            source_table="orders",
            source_column="customer_id",
            target_table="users",
            target_column="id",
        )
        table = TableInfo(
            name="orders",
            columns=[
                ColumnInfo(name="order_id", dtype="integer", is_primary_key=True),
                ColumnInfo(name="customer_id", dtype="integer"),
            ],
            foreign_keys=[fk],
        )
        assert len(table.foreign_keys) == 1
        assert table.foreign_keys[0].target_table == "users"

    def test_datasource_info(self):
        info = DatasourceInfo(
            config=DatasourceConfig(type=DatasourceType.CSV, path="/data"),
            tables=[TableInfo(name="users", columns=[ColumnInfo(name="id", dtype="integer")])],
        )
        assert len(info.tables) == 1
