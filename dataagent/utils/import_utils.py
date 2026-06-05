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
"""
Import Utilities
================

Utilities for dynamic importing of classes and modules from string paths.
"""

import importlib
from typing import Any


def import_class(class_path: str) -> type[Any]:
    """
    Import a class from a fully qualified class path string.

    This function dynamically imports a class given its full module path.
    Useful for loading classes from configuration files or at runtime.

    Args:
        class_path: Fully qualified class path in the format "module.path.ClassName"
                   Example: "mypackage.module.MyClass"

    Returns:
        The imported class object (not an instance)

    Raises:
        ValueError: If class_path is invalid or not in correct format
        ImportError: If the module cannot be imported
        AttributeError: If the class doesn't exist in the module

    Examples:
        >>> # Import a class from a string path
        >>> MyClass = import_class("mypackage.module.MyClass")
        >>> instance = MyClass()  # Create an instance

        >>> # Import standard library class
        >>> OrderedDict = import_class("collections.OrderedDict")
        >>> d = OrderedDict()

        >>> # Import from nested packages
        >>> Env = import_class("dataagent.actions.environment.env.Env")
    """
    if not class_path or not isinstance(class_path, str):
        raise ValueError(f"Invalid class path: {class_path!r}")

    # Remove leading/trailing whitespace
    class_path = class_path.strip()

    # Split into module path and class name
    parts = class_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Class path must be in format 'module.path.ClassName', got: {class_path!r}")

    module_name, class_name = parts

    # Validate that class name looks like a class (starts with capital letter)
    if not class_name or not class_name[0].isupper():
        raise ValueError(f"Class name should start with uppercase letter, got: {class_name!r}")

    try:
        # Import the module
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ImportError(f"Cannot import module '{module_name}': {e}") from e

    try:
        # Get the class from the module
        imported_class = getattr(module, class_name)
    except AttributeError as e:
        raise AttributeError(
            f"Module '{module_name}' has no class '{class_name}'. Available attributes: {', '.join(dir(module))}"
        ) from e

    # Verify it's actually a class
    if not isinstance(imported_class, type):
        raise TypeError(f"'{class_path}' is not a class, it's a {type(imported_class).__name__}")

    return imported_class


def import_callable(callable_path: str):
    """
    Import any callable (function, class, method) from a string path.

    Args:
        callable_path: Full path to callable (e.g., "os.path.join", "json.loads")

    Returns:
        The imported callable object

    Raises:
        ValueError: If callable_path is invalid
        ImportError: If the module cannot be imported
        AttributeError: If the callable doesn't exist

    Examples:
        >>> json_loads = import_callable("json.loads")
        >>> data = json_loads('{"key": "value"}')

        >>> path_join = import_callable("os.path.join")
        >>> path = path_join("/tmp", "file.txt")
    """
    if not callable_path or not isinstance(callable_path, str):
        raise ValueError(f"Invalid callable path: {callable_path!r}")

    callable_path = callable_path.strip()

    parts = callable_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Callable path must be in format 'module.path.callable', got: {callable_path!r}")

    module_name, callable_name = parts

    try:
        module = importlib.import_module(module_name)
    except ImportError as e:
        raise ImportError(f"Cannot import module '{module_name}': {e}") from e

    try:
        imported_callable = getattr(module, callable_name)
    except AttributeError as e:
        raise AttributeError(f"Module '{module_name}' has no attribute '{callable_name}'") from e

    if not callable(imported_callable):
        raise TypeError(f"'{callable_path}' is not callable")

    return imported_callable


def import_callable_from_spec(spec: str) -> Any:
    """Import a callable from ``module.path.function`` (same rules as :func:`import_callable`).

    Args:
        spec: Dotted path; the last segment is the attribute name on the imported module.

    Returns:
        The resolved callable.

    Raises:
        ValueError, ImportError, AttributeError, TypeError: Same as :func:`import_callable`.
    """
    return import_callable(spec)
