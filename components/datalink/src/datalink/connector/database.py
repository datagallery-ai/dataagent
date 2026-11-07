"""Database connector using SQLAlchemy for schema introspection."""

import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

import pandas as pd
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from datalink.connector.base import BaseConnector
from datalink.models.datasource import (
    ColumnInfo,
    DatasourceConfig,
    DatasourceInfo,
    ForeignKeyInfo,
    TableInfo,
)
from datalink.utils.sql_ident import dialect_from_source, quote_identifier

logger = logging.getLogger(__name__)


class DatabaseConnector(BaseConnector):
    """Connector for relational databases via SQLAlchemy.

    Supports PostgreSQL, MySQL, SQLite, and any database with a SQLAlchemy driver.
    Extracts tables, columns, foreign keys, and comments from the database schema.

    Schema selection: if the database name in the connection string contains a dot
    (e.g. ``mydb.myschema``), the part after the dot is extracted as the schema name
    and the connection string is cleaned to only contain the database name for
    SQLAlchemy.  For PostgreSQL this means:

        postgresql://user:pass@host/mydb.myschema  →  schema "myschema", connect to "mydb"
        postgresql://user:pass@host/mydb            →  schema "public" (default), connect to "mydb"

    Other databases (MySQL, SQLite) don't use PostgreSQL-style schemas, so
    ``db.schema`` path splitting is skipped for them.  SQLite file paths
    commonly contain dots (``.db``, ``.sqlite``) that must not be treated
    as schema separators.
    """

    # Dialects without PostgreSQL-style schemas / db.schema URL notation.
    _NO_PG_SCHEMA_DIALECTS = frozenset({"mysql", "mariadb", "sqlite"})

    def __init__(self, config: DatasourceConfig):
        super().__init__(config)
        self.engine: Engine | None = None
        # Preserve the original connection string (with db.schema notation)
        # so that TableInfo.source stores it for rebuild recovery.
        self._original_connection_string = config.connection_string

    def connect(self) -> None:
        """Create a SQLAlchemy engine from the connection string.

        Extracts the schema from ``db.schema`` notation in the connection
        string path, cleans the URL for SQLAlchemy, and stores the schema
        in ``config.schema_name``.

        For MySQL/MariaDB/SQLite (dialects that don't use PostgreSQL-style
        schemas), the default schema_name "public" is replaced with None
        so that the Inspector lists tables from the connected database
        rather than searching for a non-existent "public" schema.

        Driver fallback: if the connection string uses a bare scheme
        (e.g. ``mysql://`` or ``mariadb://``) without specifying a driver,
        SQLAlchemy defaults to ``mysqlclient``/``MySQLdb`` which is often
        not installed.  This method automatically retries with ``+pymysql``
        (or ``+psycopg2`` for PostgreSQL) if the default driver is missing.
        """
        if not self.config.connection_string:
            raise ValueError("Database connector requires a connection_string in config")

        clean_url, schema_from_url = self._extract_schema_from_url(self.config.connection_string)

        # Apply schema from URL if provided; otherwise keep the config default ("public")
        if schema_from_url:
            self.config.schema_name = schema_from_url

        # Try creating the engine; if the default driver is missing,
        # automatically retry with common alternative drivers.
        self.engine = self._create_engine_with_driver_fallback(clean_url)

        # MySQL/MariaDB/SQLite don't have PostgreSQL-style schemas.
        # Inspector.get_table_names(schema=None) lists tables in the connected db/file.
        dialect = self.engine.dialect.name
        if dialect in self._NO_PG_SCHEMA_DIALECTS and self.config.schema_name == "public":
            self.config.schema_name = None

        logger.info(f"Connected to database (schema={self.config.schema_name}): {clean_url}")

    # Driver fallback mapping: bare scheme → alternative drivers to try
    _DRIVER_FALLBACKS: dict[str, list[str]] = {
        "mysql": ["pymysql"],
        "mariadb": ["pymysql"],
        "postgresql": ["psycopg2"],
    }

    def _create_engine_with_driver_fallback(self, url: str) -> Engine:
        """Create a SQLAlchemy engine, retrying with alternative drivers
        if the default dialect driver is not installed.

        For URLs like ``mysql://user:pass@host/db`` (bare scheme, no
        ``+driver``), SQLAlchemy uses ``mysqlclient`` (MySQLdb) which is
        a C extension often missing.  This method detects the failure and
        retries with ``mysql+pymysql://`` etc.
        """
        # Check if the URL already specifies a driver (contains + in scheme)
        parsed = urlparse(url)
        scheme = parsed.scheme
        if "+" in scheme:
            # Driver already specified — try directly, no fallback
            return create_engine(url)

        base_dialect = scheme
        if base_dialect not in self._DRIVER_FALLBACKS:
            # No known fallback — try directly
            return create_engine(url)

        # Try the bare scheme first
        try:
            engine = create_engine(url)
            # Force a lightweight connection test to catch missing-driver
            # errors early (create_engine itself doesn't connect)
            with engine.connect():
                pass
            return engine
        except (ImportError, ModuleNotFoundError) as exc:
            logger.info(
                f"Default driver for '{base_dialect}' not available ({exc}), "
                f"trying fallbacks: {self._DRIVER_FALLBACKS[base_dialect]}"
            )

        # Try each fallback driver
        for driver in self._DRIVER_FALLBACKS[base_dialect]:
            fallback_url = url.replace(f"{base_dialect}://", f"{base_dialect}+{driver}://", 1)
            try:
                engine = create_engine(fallback_url)
                with engine.connect():
                    pass
                logger.info(f"Successfully connected using fallback driver '{driver}'")
                return engine
            except (ImportError, ModuleNotFoundError):
                # Driver module not installed — try next fallback
                continue
            except Exception:
                # Driver IS installed but connection failed (auth, network, etc.)
                # This is a real DB error, not a driver issue — return the
                # engine so the real error propagates to the caller.
                logger.info(
                    f"Fallback driver '{driver}' loaded but connection test failed — "
                    f"returning engine for real error to propagate"
                )
                return engine

        # All fallbacks exhausted (no driver module found for any variant)
        available = [f"{base_dialect}+{d}" for d in self._DRIVER_FALLBACKS[base_dialect]]
        raise ImportError(
            f"No usable driver found for '{base_dialect}'. "
            f" Tried '{base_dialect}' (default) and fallbacks {available}. "
            f"Install one of: pip install {base_dialect}client pymysql psycopg2-binary"
        )

    def get_datasource_info(self) -> DatasourceInfo:
        """Introspect the database schema and extract all metadata."""
        if self.engine is None:
            self.connect()

        inspector = inspect(self.engine)
        schema_name = self.config.schema_name

        # For MySQL/MariaDB, schema_name is None internally (Inspector uses
        # schema=None to list tables in the connected database).  For TableInfo
        # we store the database name from the URL so the field is meaningful
        # (e.g. "appdb") rather than None.
        table_info_schema_name = schema_name
        if schema_name is None and self.engine is not None:
            dialect = self.engine.dialect.name
            if dialect in ("mysql", "mariadb"):
                # Extract the database name from the connection URL
                parsed = urlparse(self._original_connection_string)
                db_path = parsed.path.lstrip("/")
                # Strip any .schema suffix that was extracted earlier
                if "." in db_path:
                    db_path = db_path.split(".", 1)[0]
                table_info_schema_name = db_path if db_path else None

        tables: list[TableInfo] = []
        sample_data: dict[str, list[dict[str, Any]]] = {}

        table_names = inspector.get_table_names(schema=schema_name)
        logger.info(f"Found {len(table_names)} tables in schema '{schema_name}'")

        for table_name in table_names:
            # Extract columns
            columns = self._extract_columns(inspector, table_name, schema_name)

            # Extract foreign keys
            foreign_keys = self._extract_foreign_keys(inspector, table_name, schema_name)

            # Get row count
            row_count = self._get_row_count(table_name)

            # Get table comment
            table_comment = self._get_table_comment(inspector, table_name, schema_name)

            table_info = TableInfo(
                name=table_name,
                schema_name=table_info_schema_name,
                columns=columns,
                foreign_keys=foreign_keys,
                row_count=row_count,
                comment=table_comment,
                # Use original connection string (with db.schema) so rebuild can recover schema
                source=self._original_connection_string,
            )
            tables.append(table_info)

            # Get sample data
            sample_data[table_name] = self.get_sample_data(table_name, self.config.sample_size)

        return DatasourceInfo(
            config=self.config,
            tables=tables,
            sample_data=sample_data,
        )

    def get_sample_data(self, table_name: str, n: int = 1000) -> list[dict[str, Any]]:
        """Sample rows from a table using random ordering."""
        if self.engine is None:
            self.connect()

        qualified_name = self._qualified_table_name(table_name)

        # Use RANDOM() for PostgreSQL, RAND() for MySQL, or ROWID for SQLite
        dialect = self.engine.dialect.name
        if dialect == "postgresql":
            order_clause = "ORDER BY RANDOM()"
        elif dialect == "mysql":
            order_clause = "ORDER BY RAND()"
        elif dialect == "sqlite":
            order_clause = "ORDER BY RANDOM()"
        else:
            # Fallback: no random ordering, just take first N
            order_clause = ""

        query = f"SELECT * FROM {qualified_name} {order_clause} LIMIT {n}"
        try:
            df = pd.read_sql(text(query), self.engine)
            return df.to_dict(orient="records")
        except Exception as e:
            logger.warning(f"Failed to sample table '{table_name}': {e}")
            return []

    def disconnect(self) -> None:
        """Dispose the SQLAlchemy engine."""
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None
            logger.info("Disconnected from database")

    # ── Schema / URL helpers ──────────────────────────────────────────────

    def _extract_schema_from_url(self, connection_string: str) -> tuple[str, str | None]:
        """Extract schema from ``db.schema`` notation in the connection string path.

        For PostgreSQL, a dot in the path (e.g. ``/mydb.myschema``) splits into
        database name and schema name.  Returns:

            (clean_url_with_only_dbname, schema_value_or_None)

        For MySQL/MariaDB/SQLite, there is no PostgreSQL-style schema — dots
        in the path are NOT interpreted as schema separators.  This is
        essential for SQLite, where file paths commonly contain extensions
        like ``.db`` / ``.sqlite``.

        Query parameters, netloc, etc. are preserved — only the path is modified.
        """
        parsed = urlparse(connection_string)
        path = parsed.path
        scheme = parsed.scheme

        # Determine the base dialect (strip +driver suffix)
        base_dialect = scheme.split("+")[0] if "+" in scheme else scheme

        # Dialects without PostgreSQL-style schemas — no dot splitting.
        if base_dialect in self._NO_PG_SCHEMA_DIALECTS:
            return connection_string, None

        # PostgreSQL and others: interpret dot as db.schema
        db_path = path.lstrip("/")

        if "." in db_path:
            dbname, schema = db_path.split(".", 1)
            # Rebuild path with only the database name
            new_path = f"/{dbname}" if dbname else path
            clean_url = urlunparse(
                (
                    parsed.scheme,
                    parsed.netloc,
                    new_path,
                    parsed.params,
                    parsed.query,
                    parsed.fragment,
                )
            )
            logger.info(f"Extracted schema '{schema}' from connection string path")
            return clean_url, schema

        # No dot found — no schema specification, keep URL unchanged
        return connection_string, None

    def _sql_dialect(self) -> str | None:
        """Best-effort dialect name for identifier quoting."""
        if self.engine is not None:
            return self.engine.dialect.name
        return dialect_from_source(self.config.connection_string or self._original_connection_string)

    def _quote_ident(self, name: str) -> str:
        """Quote a SQL identifier using the connected dialect when available."""
        if self.engine is not None:
            return self.engine.dialect.identifier_preparer.quote(name)
        return quote_identifier(name, self._sql_dialect())

    def _qualified_table_name(self, table_name: str) -> str:
        """Return a schema-qualified, quoted table name for raw SQL queries.

        Identifiers are always quoted so names with hyphens (e.g. ``dacomp-zh-006``)
        or reserved words work on SQLite / PostgreSQL / MySQL.

        For PostgreSQL's default "public" schema, a bare table name is enough
        because public is on the default search_path.  For any other schema,
        we prefix with the quoted schema name.
        """
        quoted_table = self._quote_ident(table_name)
        if self.config.schema_name and self.config.schema_name != "public":
            return f"{self._quote_ident(self.config.schema_name)}.{quoted_table}"
        return quoted_table

    # ── Inspector helpers ─────────────────────────────────────────────────

    def _extract_columns(self, inspector: Any, table_name: str, schema_name: str) -> list[ColumnInfo]:
        """Extract column metadata from the database inspector."""
        columns = []
        pk_columns = set()

        # Get primary key columns
        try:
            pk_info = inspector.get_pk_constraint(table_name, schema=schema_name)
            pk_columns = set(pk_info.get("constrained_columns", []))
        except Exception:
            logger.warning(f"Could not get PK constraint for '{table_name}'")

        for col in inspector.get_columns(table_name, schema=schema_name):
            columns.append(
                ColumnInfo(
                    name=col["name"],
                    dtype=str(col.get("type", "")),
                    nullable=col.get("nullable", True),
                    is_primary_key=col["name"] in pk_columns,
                    comment=col.get("comment", "") or "",
                )
            )
        return columns

    def _extract_foreign_keys(self, inspector: Any, table_name: str, schema_name: str) -> list[ForeignKeyInfo]:
        """Extract foreign key constraints from the database inspector."""
        fks = []
        try:
            fk_info = inspector.get_foreign_keys(table_name, schema=schema_name)
            for fk in fk_info:
                fks.append(
                    ForeignKeyInfo(
                        constraint_name=fk.get("name", "") or "",
                        source_table=table_name,
                        source_column=fk["constrained_columns"][0] if fk["constrained_columns"] else "",
                        target_table=fk["referred_table"],
                        target_column=fk["referred_columns"][0] if fk["referred_columns"] else "",
                    )
                )
        except Exception as e:
            logger.warning(f"Could not get FK constraints for '{table_name}': {e}")
        return fks

    def _get_row_count(self, table_name: str) -> int | None:
        """Get the row count for a table."""
        if self.engine is None:
            return None
        qualified_name = self._qualified_table_name(table_name)
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(f"SELECT COUNT(*) FROM {qualified_name}"))
                return result.scalar()
        except Exception as e:
            logger.warning(f"Could not get row count for '{table_name}': {e}")
            return None

    def _get_table_comment(self, inspector: Any, table_name: str, schema_name: str) -> str:
        """Get the table-level comment from metadata."""
        try:
            table_info = inspector.get_table_comment(table_name, schema=schema_name)
            return table_info.get("text", "") or ""
        except Exception:
            # Some dialects don't support table comments
            return ""
