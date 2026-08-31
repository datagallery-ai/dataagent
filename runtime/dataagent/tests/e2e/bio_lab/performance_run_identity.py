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

"""Run identity helpers for bio_lab performance artifacts."""

import re
from pathlib import Path


def _slugify(value: object) -> str:
    raw = str(value).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return slug or "default"


def _config_label(config_file: str | Path) -> str:
    return _slugify(Path(config_file).stem)


def build_run_parameter_label(
    *,
    model_choice: str,
    config_file: str | Path,
    quick: bool,
    skip_slow: bool,
    query_group: str = "default",
    query_numbers: list[int] | None = None,
    compress_message_cnt: int,
    recent_turns: int | None,
    cache_threshold_profile: str,
    tc2_only: bool = False,
    viz_only: bool = False,
    tool_mode: bool = False,
    query: str | None = None,
    semantic_layer_mode: str | None = None,
    semantic_layer_url: str | None = None,
) -> str:
    """Build a filesystem-safe summary of the test parameters."""
    if tool_mode:
        mode = "tool"
    elif query:
        mode = "query"
    elif viz_only:
        mode = "viz"
    elif tc2_only:
        mode = "tc2"
    elif quick:
        mode = "quick"
    elif skip_slow:
        mode = "skip-slow"
    elif query_group != "default":
        mode = _slugify(query_group)
    else:
        mode = "full"

    recent_label = "none" if recent_turns is None else str(recent_turns)
    parts = [
        mode,
        f"model-{_slugify(model_choice)}",
        f"cfg-{_config_label(config_file)}",
    ]
    if semantic_layer_mode:
        semantic_label = _slugify(semantic_layer_mode)
        if semantic_layer_url:
            semantic_label = f"{semantic_label}-{_slugify(semantic_layer_url)}"
        parts.append(f"sem-{semantic_label}")
    if query_numbers:
        parts.append(f"queries-{'-'.join(str(num) for num in query_numbers)}")
    parts.extend(
        [
            f"compress-{compress_message_cnt}",
            f"recent-{recent_label}",
            f"threshold-{_slugify(cache_threshold_profile)}",
        ]
    )
    return "__".join(parts)


def build_cache_test_ids(
    *,
    run_stamp: str,
    run_suffix: str,
    parameter_label: str,
) -> tuple[str, str]:
    label = re.sub(r"[^a-z0-9_-]+", "-", str(parameter_label).strip().lower()).strip("-_")
    label = label or "default"
    return (
        f"cache_test_user_v3_{run_stamp}_{run_suffix}_{label}",
        f"cache_test_session_v3_{run_stamp}_{run_suffix}_{label}",
    )
