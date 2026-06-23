# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""DataAgent SDK — 公开接口。"""

from dataagent.interface.sdk.agent import DataAgent
from dataagent.interface.sdk.loader import load_agent_from_config

__all__ = [
    "DataAgent",
    "load_agent_from_config",
]
