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
from typing import Any

from dataagent.actions.environment.compound_env import CompoundEnv
from dataagent.actions.environment.env import Env
from dataagent.utils.import_utils import import_class


def from_config(config: list[dict[str, Any]] | dict[str, Any]) -> Env:
    """
    Create an Env instance from configuration.

    Args:
        config: Either a single environment configuration dict or a list of dicts.
                Each dict must contain a "module" field specifying the Env subclass
                to instantiate (e.g., "mypackage.envs.MyEnv").
                Other fields in the dict are passed as keyword arguments to __init__.

    Returns:
        An Env instance. If config is a list, returns a CompoundEnv.

    Raises:
        ValueError: If configuration is invalid
        ImportError: If the specified module cannot be imported
        AttributeError: If the specified class doesn't exist in the module

    Examples:
        Single environment:
        >>> config = {
        ...     "module": "mypackage.envs.MathEnv",
        ...     "precision": 10
        ... }
        >>> env = from_config(config)

        Multiple environments (creates CompoundEnv):
        >>> config = [
        ...     {"module": "mypackage.envs.MathEnv"},
        ...     {"module": "mypackage.envs.StringEnv", "encoding": "utf-8"}
        ... ]
        >>> compound_env = from_config(config)
    """
    if isinstance(config, list):
        # Create multiple environments and combine them
        if not config:
            raise ValueError("Configuration list cannot be empty")

        envs = [_create_single_env(env_config) for env_config in config]
        return CompoundEnv(envs)
    if isinstance(config, dict):
        # Create a single environment
        return _create_single_env(config)
    raise ValueError(f"Configuration must be a dict or list of dicts, got {type(config)}")


def _create_single_env(config: dict[str, Any]) -> Env:
    """
    Create a single Env instance from a configuration dict.

    Args:
        config: Configuration dict with "module" field and optional init parameters

    Returns:
        An Env instance

    Raises:
        ValueError: If "module" field is missing
        ImportError: If module cannot be imported
        AttributeError: If class doesn't exist
        TypeError: If the class is not a subclass of Env
    """
    if "module" not in config:
        raise ValueError("Configuration dict must contain a 'module' field")

    class_path = config["module"]

    # Load the class using the utility function
    env_class = import_class(class_path)

    # Verify it's an Env subclass
    if not issubclass(env_class, Env):
        raise TypeError(f"Class {class_path} must be a subclass of Env, got {env_class}")

    # Extract init parameters (everything except "module")
    init_params = {k: v for k, v in config.items() if k != "module"}

    # Instantiate the environment
    try:
        return env_class(**init_params)
    except TypeError as e:
        raise TypeError(f"Failed to instantiate {class_path} with parameters {init_params}: {e}") from e
