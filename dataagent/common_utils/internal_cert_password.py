# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# ============================================================================
from __future__ import annotations

import os
from collections.abc import Mapping

_SQLRULES = frozenset({"sql_rules_business_twin", "sql_rules_traffic_insight"})
ENV_INBOUND_SERVER_KEY_PASSWORD = "DATAAGENT_INBOUND_SERVER_KEY_PASSWORD"
ENV_OUTBOUND_CLIENT_KEY_PASSWORD = "DATAAGENT_OUTBOUND_CLIENT_KEY_PASSWORD"


def apply_sqlrule_cert_password_env(user_sql_rules: object = None) -> None:
    """sqlrule 场景下：口令 env 未设置时从内部包写入，已在（含空白）则跳过。"""
    if str(user_sql_rules or "").strip() not in _SQLRULES:
        return
    if ENV_INBOUND_SERVER_KEY_PASSWORD in os.environ or ENV_OUTBOUND_CLIENT_KEY_PASSWORD in os.environ:
        return
    pw = require_internal_cert_password()
    os.environ[ENV_INBOUND_SERVER_KEY_PASSWORD] = pw
    os.environ[ENV_OUTBOUND_CLIENT_KEY_PASSWORD] = pw


def require_internal_cert_password() -> str:
    """从 CertManager 取 encryptKeyFilePwdContent，缺包抛 ImportError。"""
    from framework_om import CertManager
    from framework_starter import FrameworkStarter

    FrameworkStarter.init_framework()
    cert_info = CertManager.query_cert_info()
    raw = getattr(cert_info, "encryptKeyFilePwdContent", None)
    if raw is None and isinstance(cert_info, Mapping):
        raw = cert_info.get("encryptKeyFilePwdContent")
    text = str(raw).strip() if raw is not None else ""
    if not text:
        raise ValueError("encryptKeyFilePwdContent")
    return text
