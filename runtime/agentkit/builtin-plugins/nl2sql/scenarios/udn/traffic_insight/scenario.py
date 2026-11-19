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
"""traffic_insight scenario pack: wiring only.

One constant, two constructors, three hook points. The table-selection
pipeline lives in :mod:`.table_selector`; the shared dimension-join rewriter
in :mod:`..common.dimension_join`.
"""

from pathlib import Path
from typing import Any

from nl2sql.hooks import (
    GENERATOR_POST_SQL,
    PERCEPTOR_SCHEMA_CONTEXT,
    REFLECTOR_POST_SQL,
    HookContext,
)

from ..common.dimension_join import dimension_join_rewriter, load_dimensions
from .table_selector import TrafficInsightTableSelector

SEMANTIC_SCENARIO = "traffic_insight"
# 场景头（X-Semantic-Scenario）由此常量提供：build_scenario 把宿主语义客户端包装成
# 场景视图，除 health 外的所有请求自动带头；默认流程不包装，不带场景头。


def build_scenario(ctx: HookContext) -> dict[str, Any]:
    """Wire the traffic_insight pack: table selection plus the shared dimension rewriter."""
    base = Path(__file__).resolve().parent
    rewrite = dimension_join_rewriter(
        mappings=load_dimensions(base / "dimensions.yaml", SEMANTIC_SCENARIO),
        model=ctx.model,
        dialect=ctx.config.get("database.dialect", "sqlite"),
    )
    semantic = ctx.semantic_client.with_scenario(SEMANTIC_SCENARIO) if ctx.semantic_client else None
    selector = TrafficInsightTableSelector(ctx, base=base, semantic_client=semantic)
    return {
        PERCEPTOR_SCHEMA_CONTEXT: selector.select,
        GENERATOR_POST_SQL: rewrite,
        REFLECTOR_POST_SQL: rewrite,
    }
