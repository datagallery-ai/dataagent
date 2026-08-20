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
"""内部取证契约：inbound/outbound_encrypted 各自缺省开启，false 关闭。"""

from __future__ import annotations

import sys
import types

import pytest

from dataagent.common_utils.internal_cert_password import resolve_cert_key_passwords


def install_fake_internal_cert(monkeypatch, password: str = "internal-secret"):
    """注入 framework_starter / framework_om，返回调用计数。"""
    starter = types.ModuleType("framework_starter")
    om = types.ModuleType("framework_om")
    calls = {"init": 0, "query": 0, "argv": None}

    class FrameworkStarter:
        @staticmethod
        def init_framework():
            calls["init"] += 1
            calls["argv"] = list(sys.argv)

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


@pytest.mark.parametrize(
    "certificate",
    [
        None,
        {},
        {"inbound_encrypted": True, "outbound_encrypted": True},
        {"inbound_encrypted": "true", "outbound_encrypted": "true"},
    ],
)
def test_encrypted_default_or_true_fills_both_passwords(monkeypatch, certificate):
    calls = install_fake_internal_cert(monkeypatch, "from-om")
    original_argv = list(sys.argv)
    passwords = resolve_cert_key_passwords(certificate)
    assert passwords["inbound"] == "from-om"
    assert passwords["outbound"] == "from-om"
    assert calls["query"] == 1
    assert calls["init"] == 1
    assert calls["argv"][-2:] == ["-processType", "nl2sql"]
    assert sys.argv == original_argv


@pytest.mark.parametrize("raw", [False, "false", "0", "off", "no"])
def test_both_encrypted_false_skips_internal(monkeypatch, raw):
    calls = install_fake_internal_cert(monkeypatch, "from-om")
    passwords = resolve_cert_key_passwords({"inbound_encrypted": raw, "outbound_encrypted": raw})
    assert "inbound" not in passwords
    assert "outbound" not in passwords
    assert calls["init"] == 0
    assert calls["query"] == 0


@pytest.mark.parametrize("raw", [False, "false", "0", "off", "no"])
def test_inbound_encrypted_false_fills_outbound_only(monkeypatch, raw):
    calls = install_fake_internal_cert(monkeypatch, "from-om")
    passwords = resolve_cert_key_passwords({"inbound_encrypted": raw})
    assert "inbound" not in passwords
    assert passwords["outbound"] == "from-om"
    assert calls["query"] == 1


@pytest.mark.parametrize("raw", [False, "false", "0", "off", "no"])
def test_outbound_encrypted_false_fills_inbound_only(monkeypatch, raw):
    calls = install_fake_internal_cert(monkeypatch, "from-om")
    passwords = resolve_cert_key_passwords({"outbound_encrypted": raw})
    assert passwords["inbound"] == "from-om"
    assert "outbound" not in passwords
    assert calls["query"] == 1


def test_missing_package_raises_import_error():
    with pytest.raises(ImportError):
        resolve_cert_key_passwords()


def test_empty_internal_password_raises_value_error(monkeypatch):
    install_fake_internal_cert(monkeypatch, "")
    with pytest.raises(ValueError, match="encryptKeyFilePwdContent"):
        resolve_cert_key_passwords()


def test_existing_process_type_argv_not_extended(monkeypatch):
    calls = install_fake_internal_cert(monkeypatch, "from-om")
    monkeypatch.setattr(sys, "argv", ["prog", "-processType", "KEEP"])
    resolve_cert_key_passwords()
    assert calls["argv"] == ["prog", "-processType", "KEEP"]
    assert sys.argv == ["prog", "-processType", "KEEP"]
