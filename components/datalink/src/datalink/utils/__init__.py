"""Utility modules for DataLink."""

from datalink.utils.credential import (
    build_id_mapping,
    mask_credentials,
    mask_id,
    mask_result,
    resolve_masked_id,
    resolve_masked_ids,
)
from datalink.utils.ids import parse_column_id, parse_table_id
from datalink.utils.sql_ident import dialect_from_source, quote_identifier, quote_qualified

__all__ = [
    "build_id_mapping",
    "dialect_from_source",
    "mask_credentials",
    "mask_id",
    "mask_result",
    "parse_column_id",
    "parse_table_id",
    "quote_identifier",
    "quote_qualified",
    "resolve_masked_id",
    "resolve_masked_ids",
]
