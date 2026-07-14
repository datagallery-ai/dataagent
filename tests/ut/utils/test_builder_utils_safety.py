from pathlib import Path

import yaml

from dataagent.utils.builder_utils import remove_sensitive_info_of_output_yamls


def test_remove_sensitive_info_masks_common_secret_keys(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "api_key": "sk-visible",
                "base_url": "https://private.example",
                "nested": {
                    "password": "plain-password",
                    "access_token": "plain-token",
                    "authorization": "Bearer plain-token",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    remove_sensitive_info_of_output_yamls(output_dir=tmp_path)

    raw = config_path.read_text(encoding="utf-8")
    assert "sk-visible" not in raw
    assert "https://private.example" in raw
    assert "plain-password" not in raw
    assert "plain-token" not in raw
