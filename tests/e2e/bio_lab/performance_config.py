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

"""Config, path, and model helpers for the bio_lab performance e2e test."""

import os
from pathlib import Path

from performance_semantic_mock import _DEFAULT_AGENT_CONFIG_FILE, _apply_semantic_layer_config

BIO_LAB_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BIO_LAB_DIR / "config"


# ---------------------------------------------------------------------------
# Config path resolver
# ---------------------------------------------------------------------------
def _resolve_path(value: str, base: Path) -> str:
    """Resolve a relative path to absolute using *base* as root."""
    p = Path(value)
    if p.is_absolute():
        return value
    return str((base / p).resolve())


def _resolve_config_paths(config: dict, workspace_dir: Path) -> dict:
    """Resolve relative paths and placeholders in known config fields."""
    resolved = config.copy()

    workspace = resolved.get("WORKSPACE", {})
    if isinstance(workspace, dict):
        path_val = workspace.get("path", "")
        if path_val == "__WORKSPACE_DIR__":
            workspace["path"] = str(workspace_dir)
        allow_path = workspace.get("allow_path", [])
        if isinstance(allow_path, list):
            workspace["allow_path"] = [
                str(workspace_dir) if p == "__WORKSPACE_DIR__" else _resolve_path(p, BIO_LAB_DIR) for p in allow_path
            ]

    database = resolved.get("DATABASE", {})
    if isinstance(database, dict):
        db_config = database.get("config", {})
        if isinstance(db_config, dict) and "path" in db_config:
            path_val = db_config["path"]
            if "__WORKSPACE_DIR__" in path_val:
                db_config["path"] = path_val.replace("__WORKSPACE_DIR__", str(workspace_dir))

    tools = resolved.get("TOOLS", {})
    if isinstance(tools, dict):
        skills = tools.get("skills", {})
        if isinstance(skills, dict):
            custom_dirs = skills.get("custom_dirs", [])
            if isinstance(custom_dirs, list):
                skills["custom_dirs"] = [_resolve_path(d, BIO_LAB_DIR) for d in custom_dirs]

    return resolved


def _resolve_agent_config_path(config_file: str | Path) -> Path:
    """Resolve an agent config YAML path for this e2e suite."""
    path = Path(config_file)
    if not path.is_absolute():
        candidate = CONFIG_DIR / path
        path = candidate if candidate.exists() else (Path.cwd() / path)
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"Agent config YAML not found: {path}")
    if path.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError(f"Agent config must be a YAML file: {path}")
    return path


def _load_agent_config(config_file: str | Path) -> dict:
    """Load the requested agent config YAML."""
    import yaml

    config_path = _resolve_agent_config_path(config_file)
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


_ORIGINAL_SQLITE_PATH = Path(
    os.environ.get("BIO_LAB_ORIGINAL_SQLITE_PATH", str(BIO_LAB_DIR / "data" / "bio_lab.sqlite"))
).expanduser()

# Model presets for the cache test. Each entry maps a `--model` CLI choice to
# the YAML MODEL.chat_model fields. `base_url_env` / `api_key_env` name the
# environment variables that the test resolves at config-build time so the
# generated YAML stays self-contained.
MODEL_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "base_url_env": "DEEPSEEK_BASE_URL",
        "api_key_env": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "provider": "openai",
        "model": "Qwen3.7-Plus",
        "base_url_env": "OPENAI_BASE_URL",
        "api_key_env": "OPENAI_API_KEY",
    },
    "bailian": {
        "provider": "bailian",
        "model": "qwen3.7-plus",
        "base_url_env": "BAILIAN_BASE_URL",
        "api_key_env": "BAILIAN_API_KEY",
    },
}
DEFAULT_MODEL_CHOICE = "deepseek"
DEFAULT_COMPRESS_MESSAGE_CNT = 200
DEFAULT_RECENT_TURNS = 200


def _resolve_model_preset(model_choice: str) -> dict[str, str]:
    """Return the model preset for ``model_choice`` or raise ValueError."""
    if model_choice not in MODEL_PRESETS:
        raise ValueError(f"Unknown model choice: {model_choice!r}. Supported: {sorted(MODEL_PRESETS)}")
    return dict(MODEL_PRESETS[model_choice])


def _apply_model_choice(config: dict, model_choice: str) -> dict:
    """Override config["MODEL"]["chat_model"] based on ``model_choice``.

    Writes ``base_url`` / ``api_key`` as ``$env{...}`` placeholders so the
    ConfigManager resolves them from ``.env`` at load time (consistent with
    the original ``main_config.yaml``).
    """
    preset = _resolve_model_preset(model_choice)
    model_cfg = config.setdefault("MODEL", {}).setdefault("chat_model", {})
    model_cfg["model_type"] = "chat"
    model_cfg["provider"] = preset["provider"]
    model_cfg.setdefault("params", {})
    model_cfg["params"]["model"] = preset["model"]
    model_cfg["params"]["base_url"] = f"$env{{{preset['base_url_env']}}}"
    model_cfg["params"]["api_key"] = f"$env{{{preset['api_key_env']}}}"
    model_cfg["params"].setdefault("temperature", 0)
    return config


def _build_cache_test_config(
    workspace_dir: Path,
    compress_message_cnt: int = DEFAULT_COMPRESS_MESSAGE_CNT,
    compress_token_limit: int = 128000,
    enable_human_feedback: bool = True,
    session_root: Path | None = None,
    model_choice: str = DEFAULT_MODEL_CHOICE,
    recent_turns: int | None = DEFAULT_RECENT_TURNS,
    config_file: str | Path = _DEFAULT_AGENT_CONFIG_FILE,
) -> Path:
    import yaml

    config = _load_agent_config(config_file)
    config = _resolve_config_paths(config, workspace_dir)
    # Ontology (get_ontology_description) and NL2SQL perceptor both go through
    # SemanticServiceClient reading SEMANTIC_LAYER.base_url. 默认走内联 mock
    # server（离线可复现，三个基础检索 REST 端点齐全）；CLI 可显式切到
    # 真实 semantic-service URL。
    _apply_semantic_layer_config(config)
    context_cfg = config.setdefault("CONTEXT", {})
    context_cfg["compress_message_cnt"] = compress_message_cnt
    context_cfg["compress_token_limit"] = compress_token_limit
    if recent_turns is not None:
        context_cfg["recent_turns"] = recent_turns

    _apply_model_choice(config, model_choice)

    config.setdefault("AGENT_CONFIG", {})["enable_human_feedback"] = enable_human_feedback

    dump_dir = session_root if session_root is not None else workspace_dir
    dump_dir.mkdir(parents=True, exist_ok=True)
    out_path = dump_dir / "test_cache_v3.yaml"
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    return out_path
