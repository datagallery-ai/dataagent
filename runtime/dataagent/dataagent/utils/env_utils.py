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
"""Environment variable helpers for ``DATAAGENT_*`` names."""

from __future__ import annotations

import os


def get_env(name: str, *, default: str | None = None) -> str | None:
    """Read an environment variable by name."""
    return os.getenv(name, default)


def get_env_bool(name: str, *, default: bool = False) -> bool:
    """Parse a boolean env var; truthy values: ``1``, ``true``, ``yes``, ``on`` (case-insensitive)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def set_env(name: str, value: str) -> None:
    """Set an environment variable."""
    os.environ[name] = value
