from __future__ import annotations

import pytest
from datafoundry_api.settings import Settings


def test_default_bind_is_loopback() -> None:
    settings = Settings.from_env({})
    assert settings.host == "127.0.0.1"
    assert settings.port == 8787


def test_public_bind_hosts_are_accepted() -> None:
    for host in ("0.0.0.0", "::", "192.168.1.10", "example.com", "[::]", "127.0.0.1", "localhost"):
        settings = Settings.from_env({"API_HOST": host, "API_PORT": "8787"})
        assert settings.port == 8787
        assert settings.host == host.strip("[]")


def test_blank_host_is_rejected() -> None:
    for host in ("", "   ", "127.0.0.1/32"):
        with pytest.raises(ValueError, match="API_HOST"):
            Settings.from_env({"API_HOST": host})


def test_port_is_validated() -> None:
    with pytest.raises(ValueError, match="API_PORT"):
        Settings.from_env({"API_PORT": "0"})
    with pytest.raises(ValueError, match="API_PORT"):
        Settings.from_env({"API_PORT": "70000"})
