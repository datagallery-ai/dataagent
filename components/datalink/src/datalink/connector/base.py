"""Base connector interface for data sources."""

from abc import ABC, abstractmethod

from datalink.models.datasource import DatasourceConfig, DatasourceInfo


class BaseConnector(ABC):
    """Abstract base class for data source connectors.

    Each connector type (database, file) implements this interface to
    extract schema metadata and sample data from a data source.
    """

    def __init__(self, config: DatasourceConfig):
        self.config = config

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the data source."""
        ...

    @abstractmethod
    def get_datasource_info(self) -> DatasourceInfo:
        """Extract complete datasource info: tables, columns, FKs, comments, sample data."""
        ...

    @abstractmethod
    def get_sample_data(self, table_name: str, n: int = 1000) -> list[dict]:
        """Get sample rows from a specific table.

        Args:
            table_name: Name of the table to sample.
            n: Number of rows to sample.

        Returns:
            List of dicts, each representing a row.
        """
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection to the data source."""
        ...
