# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# ============================================================================
"""Package-root SDK export tests."""

from __future__ import annotations

import subprocess
import sys

import pytest


def test_package_root_exports_public_sdk() -> None:
    from dataagent import DataAgent, load_agent_from_config
    from dataagent.interface.sdk import (
        DataAgent as SdkDataAgent,
    )
    from dataagent.interface.sdk import (
        load_agent_from_config as sdk_load_agent_from_config,
    )

    assert DataAgent is SdkDataAgent
    assert load_agent_from_config is sdk_load_agent_from_config


@pytest.mark.parametrize("removed_name", ["AgentBuilder", "BaseDataAgent"])
def test_package_root_does_not_restore_removed_builder_exports(removed_name: str) -> None:
    import dataagent

    with pytest.raises(AttributeError):
        getattr(dataagent, removed_name)


def test_package_root_all_contains_only_supported_sdk_exports() -> None:
    import dataagent

    namespace: dict[str, object] = {}
    exec("from dataagent import *", namespace)

    assert dataagent.__all__ == ["DataAgent", "load_agent_from_config"]
    assert namespace["DataAgent"] is dataagent.DataAgent
    assert namespace["load_agent_from_config"] is dataagent.load_agent_from_config
    assert "AgentBuilder" not in namespace
    assert "BaseDataAgent" not in namespace


def test_package_import_and_lazy_exports_do_not_import_openjiuwen() -> None:
    script = """
import sys
import dataagent

assert "dataagent.interface.sdk" not in sys.modules
assert not any(name == "openjiuwen" or name.startswith("openjiuwen.") for name in sys.modules)

_ = dataagent.DataAgent

assert "dataagent.interface.sdk" in sys.modules
assert not any(name == "openjiuwen" or name.startswith("openjiuwen.") for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], check=True)
