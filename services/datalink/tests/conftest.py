"""Shared test fixtures for DataLink tests."""

from pathlib import Path

import pytest

# Path to test data directory
TEST_DATA_DIR = Path(__file__).parent / "test_data"


@pytest.fixture
def users_csv_path() -> Path:
    """Path to the test users.csv file."""
    return TEST_DATA_DIR / "users.csv"


@pytest.fixture
def orders_csv_path() -> Path:
    """Path to the test orders.csv file."""
    return TEST_DATA_DIR / "orders.csv"


@pytest.fixture
def transactions_csv_path() -> Path:
    """Path to the test transactions.csv file."""
    return TEST_DATA_DIR / "transactions.csv"


@pytest.fixture
def all_csv_paths() -> list[Path]:
    """Paths to all test CSV files."""
    return [
        TEST_DATA_DIR / "users.csv",
        TEST_DATA_DIR / "orders.csv",
        TEST_DATA_DIR / "transactions.csv",
    ]
