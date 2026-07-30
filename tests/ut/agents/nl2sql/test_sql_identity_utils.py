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
from dataagent.agents.nl2sql.utils.nl2sql_utils import new_trace_id, normalize_sql, sql_sha256


def test_normalize_and_hash_stable():
    a = sql_sha256("SELECT   1")
    b = sql_sha256("SELECT 1")
    assert a == b
    assert normalize_sql("  SELECT\n1 ") == "SELECT 1"


def test_new_trace_id_is_hex():
    tid = new_trace_id()
    assert len(tid) == 32
    int(tid, 16)
