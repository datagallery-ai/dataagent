import pytest

from dataagent.core.framework_adapters.checkpoints.postgres_store import PostgresCheckpointStore
from dataagent.core.framework_adapters.checkpoints.sqlite_store import SqliteCheckpointStore


@pytest.mark.parametrize("store_cls,args", [(SqliteCheckpointStore, (":memory:",)), (PostgresCheckpointStore, ("",))])
def test_checkpoint_store_rejects_unsafe_table_name_before_sql_execution(store_cls, args) -> None:
    with pytest.raises(ValueError, match="Invalid checkpoint table name"):
        store_cls(*args, table_name="checkpoints; DROP TABLE users;--")
