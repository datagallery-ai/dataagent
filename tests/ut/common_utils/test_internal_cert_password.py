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
"""内部取证契约：两场景写双 env，已填跳过，缺包 ImportError。"""

from __future__ import annotations

import os
import sys
import types

import pytest

from dataagent.common_utils.internal_cert_password import apply_sqlrule_cert_password_env

_INBOUND_ENV = "DATAAGENT_INBOUND_SERVER_KEY_PASSWORD"
_OUTBOUND_ENV = "DATAAGENT_OUTBOUND_CLIENT_KEY_PASSWORD"


def install_fake_internal_cert(monkeypatch, password: str = "internal-secret"):
    """注入 framework_starter / framework_om，返回调用计数。"""
    starter = types.ModuleType("framework_starter")
    om = types.ModuleType("framework_om")
    calls = {"init": 0, "query": 0}

    class FrameworkStarter:
        @staticmethod
        def init_framework():
            calls["init"] += 1

    class CertManager:
        @staticmethod
        def query_cert_info():
            calls["query"] += 1
            return types.SimpleNamespace(encryptKeyFilePwdContent=password)

    starter.FrameworkStarter = FrameworkStarter
    om.CertManager = CertManager
    monkeypatch.setitem(sys.modules, "framework_starter", starter)
    monkeypatch.setitem(sys.modules, "framework_om", om)
    return calls


def _clear_password_env() -> None:
    os.environ.pop(_INBOUND_ENV, None)
    os.environ.pop(_OUTBOUND_ENV, None)


@pytest.fixture(autouse=True)
def _reset_adapter():
    _clear_password_env()
    yield
    _clear_password_env()


@pytest.mark.parametrize("rules", ["sql_rules_business_twin", "sql_rules_traffic_insight"])
def test_sqlrule_writes_both_password_env(monkeypatch, rules):
    calls = install_fake_internal_cert(monkeypatch, "from-om")
    apply_sqlrule_cert_password_env(rules)
    assert os.environ[_INBOUND_ENV] == "from-om"
    assert os.environ[_OUTBOUND_ENV] == "from-om"
    assert calls["query"] == 1


def test_sqlrule_skips_internal_when_password_env_already_set(monkeypatch):
    """已有明文口令则跳过内部包，不是缺包软回落。"""
    calls = install_fake_internal_cert(monkeypatch, "from-om")
    os.environ[_INBOUND_ENV] = "plain-inbound"
    os.environ[_OUTBOUND_ENV] = "plain-outbound"
    apply_sqlrule_cert_password_env("sql_rules_business_twin")
    assert os.environ[_INBOUND_ENV] == "plain-inbound"
    assert os.environ[_OUTBOUND_ENV] == "plain-outbound"
    assert calls["init"] == 0
    assert calls["query"] == 0


def test_sqlrule_existing_env_skips_internal_even_without_package():
    """已有口令跳过：不装包也不会 ImportError。"""
    os.environ[_INBOUND_ENV] = "plain-secret"
    os.environ[_OUTBOUND_ENV] = "plain-secret"
    apply_sqlrule_cert_password_env("sql_rules_traffic_insight")
    assert os.environ[_INBOUND_ENV] == "plain-secret"
    assert os.environ[_OUTBOUND_ENV] == "plain-secret"


def test_sqlrule_without_package_raises_import_error():
    with pytest.raises(ImportError):
        apply_sqlrule_cert_password_env("sql_rules_business_twin")


def test_non_sqlrule_does_not_import_internal_package(monkeypatch):
    calls = install_fake_internal_cert(monkeypatch, "from-om")
    apply_sqlrule_cert_password_env("sql_rules_bird")
    assert _INBOUND_ENV not in os.environ
    assert _OUTBOUND_ENV not in os.environ
    assert calls["init"] == 0
    assert calls["query"] == 0


def test_sqlrule_blank_env_skips_internal_package(monkeypatch):
    """变量已在（即便空白）当已接管：不调内部包，TLS 仍把空口令当没有。"""
    calls = install_fake_internal_cert(monkeypatch, "from-om")
    os.environ[_INBOUND_ENV] = ""
    os.environ[_OUTBOUND_ENV] = "   "
    apply_sqlrule_cert_password_env("sql_rules_business_twin")
    assert os.environ[_INBOUND_ENV] == ""
    assert os.environ[_OUTBOUND_ENV] == "   "
    assert calls["init"] == 0
    assert calls["query"] == 0
