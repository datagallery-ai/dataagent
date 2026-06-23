# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""DataAgent YAML configuration.

Create an isolated :class:`ConfigManager` per Agent or script::

    cm = ConfigManager()
    cm.reload("agent.yaml", default_config_path="flex_default_configs.yaml")
"""

__all__ = [
    "ConfigManager",
    "create_config_manager",
]

from dataagent.config.config_manager import ConfigManager


def create_config_manager(config_path=None):
    """Create a new isolated :class:`ConfigManager` instance."""
    return ConfigManager(config_path)
