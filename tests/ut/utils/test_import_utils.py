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
import pytest

from dataagent.utils.import_utils import import_class


def test_import_class_rejects_modules_outside_dataagent_namespace() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        import_class("pathlib.Path")


def test_import_class_allows_dataagent_namespace() -> None:
    cls = import_class("dataagent.core.cbb.agent_env.Env")

    assert cls.__name__ == "Env"
