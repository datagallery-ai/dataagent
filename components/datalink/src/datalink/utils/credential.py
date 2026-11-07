"""Credential masking utilities — hide username/password in database connection strings.

When DataLink is used as a semantic layer (especially via MCP), node IDs and
source strings embed database connection strings like
postgresql://admin:s3cret@host/db.  These helpers mask the credential parts
so that agent-facing output never leaks usernames or passwords, while keeping
scheme/host/port/db_name intact for context.

Two modes:
- mask_credential=True  (default for MCP / public retrieval) — output masked
- mask_credential=False — output raw (for trusted contexts or when the agent
  needs the actual connection string to connect to the database itself)
"""

import re
from typing import Any
from urllib.parse import urlparse

# ── URL credential masking ──────────────────────────────────────────────

# Pattern: anything that looks like a database URL scheme
# Supports both bare scheme (postgresql://) and dialect+driver (postgresql+psycopg2://).
_DB_URL_SCHEME = r"(?:postgresql|mysql|sqlite|mssql|oracle|clickhouse|mariadb)(?:\+\w+)?"
_DB_URL_RE = re.compile(rf"^{_DB_URL_SCHEME}://", re.IGNORECASE)


def mask_credentials(value: str) -> str:
    """Mask username and password in a database connection string.

    Preserves scheme, host, port, database name, and query params.
    Replaces userinfo (user:pass@) with ***:***@.

    Non-URL strings (file paths, plain names) are returned unchanged.

    Examples:
        postgresql://admin:secret123@db.example.com:5432/mydb
        → postgresql://***:***@db.example.com:5432/mydb

        postgresql://user@host/db
        → postgresql://***@host/db

        sqlite:///path/to/db.sqlite
        → sqlite:///path/to/db.sqlite  (no userinfo to mask)

        /home/user/data/orders.csv
        → /home/user/data/orders.csv  (not a DB URL)
    """
    if not value or not _DB_URL_RE.match(value):
        return value

    # Use regex to mask the userinfo part (anything between scheme:// and @host)
    # This handles all edge cases: user:pass@, user@, :pass@, etc.
    # Supports dialect+driver schemes like mysql+pymysql://.
    masked = re.sub(
        rf"({_DB_URL_SCHEME})://([^@]+)@",
        r"\1://***:***@",
        value,
        count=1,
        flags=re.IGNORECASE,
    )

    # If the regex didn't match (e.g. sqlite:///path with no @), return original
    if masked == value:
        return value

    # For URLs like "postgresql://user@host" (no password), the regex
    # replaced "user@" with "***:***@", but the original had no colon.
    # Fix: if original had no password (no second colon before @), use single ***
    # Check by seeing if the original userinfo had a colon
    try:
        parsed = urlparse(value)
        if not parsed.password and parsed.username:
            # Replace the "***:***@" with "***@" for user-only URLs
            masked = masked.replace("***:***@", "***@")
    except Exception:
        pass

    return masked


# ── Node ID masking ──────────────────────────────────────────────────────

# Node ID format: type:source_name:table_name[:col_name]
# Only the source_name part (2nd segment) may contain credentials.
# We must be careful: source_name for file paths can contain colons
# (e.g. C:\Users\...) or be a long URL.  The format is:
#   prefix:type_prefix:rest_of_id
# where type_prefix is "table" or "column" or "edge" or "profile".
# The source_name is everything between the type_prefix and the
# LAST segment(s): for tables it's "table:<source>:<table_name>",
# for columns "column:<source>:<table_name>:<col_name>",
# for edges it varies.
#
# Strategy: split on the type prefix to find boundaries, then
# identify the source segment position based on node type.

# Known ID prefixes and the position of source_name in the colon-split
# "table"  → ["table", source_name, table_name]           — source at index 1
# "column" → ["column", source_name, table_name, col_name] — source at index 1
# "edge"   → varies, but source may appear at index 1 or deeper
# "profile" → ["profile", "column:<source>:...:col_name"]  — embedded column ID
# "pending" → similar structure

# Simpler approach: since source_name is always at position 1 after
# splitting on the first colon, and it ends where the next "known"
# delimiter begins, we use a regex approach that finds the source_name
# segment in the ID.


def mask_id(node_id: str) -> str:
    """Mask credentials in a node/edge/profile ID string.

    IDs are formatted as type:source_name:... where source_name may
    be a database connection string.  This function masks only the
    credential parts (user:pass@), leaving the rest intact.

    For simple IDs (table, column), the source_name is identified by
    position and masked precisely.  For complex IDs (edge, profile),
    we fall back to a regex that masks all embedded connection strings.

    Examples:
        table:postgresql://admin:secret@host/db:orders
        → table:postgresql://***:***@host/db:orders

        column:postgresql://admin:secret@host/db:orders:customer_id
        → column:postgresql://***:***@host/db:orders:customer_id

        edge:contains:table:postgresql://admin:pass@host/db:orders:...
        → edge:contains:table:postgresql://***:***@host/db:orders:...

        table:/home/user/data:orders
        → table:/home/user/data:orders  (no credentials)
    """
    if not node_id:
        return node_id

    # Split into type prefix and the rest
    first_colon = node_id.find(":")
    if first_colon == -1:
        return node_id

    type_prefix = node_id[:first_colon]

    # For "profile" IDs: "profile:column:source:..." — double-nested
    # Mask the inner column ID recursively
    if type_prefix == "profile":
        inner_id = node_id[first_colon + 1 :]
        masked_inner = mask_id(inner_id)
        return f"profile:{masked_inner}"

    # For "table" and "column" IDs — source_name is at a known position:
    #   table:<source_name>:<table_name>
    #   column:<source_name>:<table_name>:<col_name>
    # The source_name can contain colons (URL scheme), so we find the
    # boundary by looking at the last or second-last colon.
    if type_prefix in ("table", "column"):
        rest = node_id[first_colon + 1 :]
        last_colon = rest.rfind(":")
        if last_colon == -1:
            # Only one segment — nothing to mask
            return node_id

        source_name = rest[:last_colon]
        tail = rest[last_colon + 1 :]

        # For column IDs, need second-last colon to separate source from table_name
        if type_prefix == "column":
            second_last_colon = rest.rfind(":", 0, last_colon)
            if second_last_colon > 0:
                source_name = rest[:second_last_colon]
                tail = rest[second_last_colon + 1 :]

        masked_source = mask_credentials(source_name)
        return f"{type_prefix}:{masked_source}:{tail}"

    # For all other IDs (edge, concept, entity, etc.) — the ID format
    # is more complex.  Use regex to find and mask all embedded
    # connection strings within the ID string.
    return _mask_embedded_urls(node_id)


# Regex to find DB URL patterns embedded in a string and mask their userinfo
_EMBEDDED_URL_RE = re.compile(
    rf"({_DB_URL_SCHEME})://([^@]+)@",
    re.IGNORECASE,
)


def _mask_embedded_urls(text: str) -> str:
    """Mask all embedded database URL userinfo in a string.

    Used as a fallback for complex IDs where source_name position
    is not deterministic (e.g. edge IDs with embedded table/column IDs).
    """

    def _replace(match):
        scheme = match.group(1)
        userinfo = match.group(2)
        # Check if userinfo has a colon (user:pass) or just user
        if ":" in userinfo:
            return f"{scheme}://***:***@"
        else:
            return f"{scheme}://***@"

    return _EMBEDDED_URL_RE.sub(_replace, text)


# ── Recursive result masking ────────────────────────────────────────────

# Keys whose values should be masked as node IDs
_ID_KEYS = {"id", "source_id", "target_id", "other_id", "table_id", "profile_id"}

# Keys whose values should be masked as credential-bearing strings
_CREDENTIAL_KEYS = {"source", "connection_string"}

# Keys whose values are lists of node IDs (each element masked via mask_id)
_ID_LIST_KEYS = {"column_ids"}

# Keys whose values are dicts that may contain credential-bearing entries
_PROPERTIES_KEYS = {"properties", "other_node"}


def mask_result(data: Any) -> Any:
    """Recursively mask credentials in a retrieval result structure.

    Walks through dicts and lists, masking:
    - ID-like fields (id, source_id, target_id, table_id, profile_id) via mask_id()
    - Credential fields (source, connection_string) via mask_credentials()
    - ID list fields (column_ids) element-wise via mask_id()
    - Nested dicts (properties, other_node) recursively
    - Any other string containing a DB URL pattern via _mask_embedded_urls

    Returns a new structure — does not mutate the input.
    """
    if isinstance(data, str):
        # Standalone strings may be node IDs or source strings.
        # _DB_URL_RE has a ^ anchor so won't match embedded URLs.
        # Use _EMBEDDED_URL_RE (no anchor) for detection, then mask.
        if _EMBEDDED_URL_RE.search(data):
            return _mask_embedded_urls(data)
        return data

    if isinstance(data, list):
        return [mask_result(item) for item in data]

    if isinstance(data, dict):
        masked = {}
        for key, value in data.items():
            if key in _ID_KEYS and isinstance(value, str):
                masked[key] = mask_id(value)
            elif key in _CREDENTIAL_KEYS and isinstance(value, str):
                masked[key] = mask_credentials(value)
            elif key in _ID_LIST_KEYS and isinstance(value, list):
                masked[key] = [mask_id(item) if isinstance(item, str) else mask_result(item) for item in value]
            elif key in _PROPERTIES_KEYS and isinstance(value, dict):
                masked[key] = mask_result(value)
            elif isinstance(value, (dict, list)):
                masked[key] = mask_result(value)
            elif isinstance(value, str):
                # Catch-all: any string value that contains a DB URL pattern
                # must be masked.  This prevents leaks in arbitrary properties
                # fields that aren't in _ID_KEYS / _CREDENTIAL_KEYS.
                # _mask_embedded_urls handles both standalone and embedded URLs.
                if _EMBEDDED_URL_RE.search(value):
                    masked[key] = _mask_embedded_urls(value)
                else:
                    masked[key] = value
            else:
                masked[key] = value
        return masked

    # int, float, bool, None, etc. — pass through
    return data


# ── Masked ID → real ID resolution ──────────────────────────────────────


def is_masked_id(node_id: str) -> bool:
    """Check if a node ID contains masked credentials.

    Detects patterns like "***:***@" or "***@" that are produced by
    mask_credentials / mask_id.
    """
    if not node_id:
        return False
    # Check for masked userinfo patterns: "***:***@" or "***@"
    # "***@" could be part of "***@host" — look for "***@" preceded by ://
    return "***:***@" in node_id or "://***@" in node_id


def build_id_mapping(all_node_ids: set[str]) -> dict[str, str]:
    """Build a bidirectional mapping: masked_id → real_id.

    For each real node ID, compute its masked version and store the pair.
    Overwrites are safe because mask_id is deterministic — the same real ID
    always produces the same masked ID.
    """
    mapping: dict[str, str] = {}
    for real_id in all_node_ids:
        masked = mask_id(real_id)
        if masked != real_id:  # only store if masking actually changed something
            mapping[masked] = real_id
    return mapping


def resolve_masked_id(node_id: str, mapping: dict[str, str]) -> str:
    """Resolve a possibly-masked node ID back to the real ID.

    If the ID is not masked (doesn't contain ***), returns it unchanged.
    If the ID is masked and found in the mapping, returns the real ID.
    If the ID is masked but NOT in the mapping, returns it as-is
    (the caller should handle this as a "not found" case).
    """
    if not is_masked_id(node_id):
        return node_id

    return mapping.get(node_id, node_id)


def resolve_masked_ids(node_ids: list[str], mapping: dict[str, str]) -> list[str]:
    """Resolve a list of possibly-masked node IDs."""
    return [resolve_masked_id(nid, mapping) for nid in node_ids]
